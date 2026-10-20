"""Idempotent email notifications for interview sessions.

Every email is one Grid Interview Notification Log row with a unique idempotency key
(sha256 of a logical key such as "reminder:<session>:<offset>:<start>:<recipient>").
A second attempt to send the same logical notification is a no-op, so scheduler re-runs,
double clicks and worker restarts cannot produce duplicate emails.

Delivery uses Frappe's Email Queue (configured outgoing account). Status flow:
Queued (handed to Email Queue) -> Sent (Email Queue reports Sent) | Failed (attempts exhausted)
| Skipped (disabled / not deliverable / superseded) | Cancelled.
"Sent" means the SMTP server accepted the message; delivery to the inbox is not confirmed.
"""

import hashlib
import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import (
	add_to_date,
	cint,
	escape_html,
	format_date,
	format_time,
	get_datetime,
	get_url,
	get_url_to_form,
	now_datetime,
)

from grid_interview.integrations import hrms_adapter
from grid_interview.utils.settings import get_settings

LOG = "Grid Interview Notification Log"
SESSION = "Grid Interview Video Session"

SCHEDULED = "interview_scheduled"
RESCHEDULED = "interview_rescheduled"
CANCELLED = "interview_cancelled"
REMINDER = "interview_reminder"
ACCESS_LINK = "interview_access_link"

CANDIDATE = "Candidate"
INTERVIEWER = "Interviewer"

TOKEN_LINK_TYPES = {"portal_reference_verification", ACCESS_LINK}

TEMPLATE_DIR = "grid_interview/templates/emails"


def _subject(notification_type: str, audience: str, ctx: dict) -> str:
	job = ctx.get("job_title") or ctx.get("interview_type") or _("Interview")
	when = f"{ctx.get('date')} {ctx.get('start_time')}"
	if audience == CANDIDATE:
		return {
			SCHEDULED: _("Interview scheduled: {0} on {1}").format(job, when),
			RESCHEDULED: _("Interview rescheduled: {0}, now on {1}").format(job, when),
			CANCELLED: _("Interview cancelled: {0}").format(job),
			REMINDER: _("Reminder: your interview for {0} on {1}").format(job, when),
			ACCESS_LINK: _("Your personal link to join the interview for {0}").format(job),
		}[notification_type]
	name = ctx.get("candidate_name") or _("candidate")
	return {
		SCHEDULED: _("You are interviewing {0} ({1}) on {2}").format(name, job, when),
		RESCHEDULED: _("Rescheduled: interview with {0}, now on {1}").format(name, when),
		CANCELLED: _("Cancelled: interview with {0} ({1})").format(name, job),
		REMINDER: _("Reminder: interview with {0} on {1}").format(name, when),
	}[notification_type]


def key_hash(logical_key: str) -> str:
	return hashlib.sha256(logical_key.encode()).hexdigest()


# --------------------------------------------------------------------------- public API
def notify(
	notification_type: str,
	reference_name: str,
	recipient: str,
	audience: str,
	logical_key: str,
	extra: dict | None = None,
	scheduled_for=None,
	reference_doctype: str = SESSION,
) -> str | None:
	"""Create the notification once and hand it to the Email Queue. Returns the log name,
	or None when this logical notification already exists."""
	key = key_hash(logical_key)
	if frappe.db.exists(LOG, {"idempotency_key": key}):
		return None
	try:
		log = frappe.get_doc(
			{
				"doctype": LOG,
				"idempotency_key": key,
				"notification_type": notification_type,
				"audience": audience,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"recipient": recipient,
				"status": "Queued",
				"attempts": 0,
				"scheduled_for": scheduled_for,
				"context_json": json.dumps(extra or {}, default=str),
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return None  # a concurrent worker created it first
	deliver(log)
	return log.name


def record_skipped(notification_type, session_name, recipient, audience, logical_key, reason):
	key = key_hash(logical_key)
	if frappe.db.exists(LOG, {"idempotency_key": key}):
		return
	try:
		frappe.get_doc(
			{
				"doctype": LOG,
				"idempotency_key": key,
				"notification_type": notification_type,
				"audience": audience,
				"reference_doctype": SESSION,
				"reference_name": session_name,
				"recipient": recipient,
				"status": "Skipped",
				"last_error": reason,
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		pass


def deliver(log) -> None:
	"""(Re)render and queue one email for an existing log row. Never raises."""
	settings = get_settings()
	if not cint(settings.enable_email_notifications):
		_finish(log, "Skipped", error=_("Email notifications are disabled in Grid Interview Settings"))
		return
	max_attempts = cint(settings.max_email_attempts) or 3
	log.attempts = cint(log.attempts) + 1
	try:
		subject, message = render(log)
		queue = frappe.sendmail(
			recipients=[log.recipient],
			subject=subject,
			message=message,
			reference_doctype=log.reference_doctype,
			reference_name=log.reference_name,
			add_unsubscribe_link=0,  # transactional: interview notices must not be unsubscribable
			delayed=True,
			# emails carrying a one-time link: Frappe removes the body from Email Queue once sent
			redact_message_after_send=log.notification_type in TOKEN_LINK_TYPES,
		)
	except Exception as e:
		frappe.log_error(title=f"grid_interview email failed: {log.notification_type}")
		_finish(log, "Failed" if log.attempts >= max_attempts else "Queued", error=_short(e), email_queue=None)
		return
	if not queue:
		_finish(log, "Skipped", error=_("Recipient is not deliverable (disabled user or unsubscribed address)"))
		return
	_finish(log, "Queued", error=None, email_queue=queue.name)


def _finish(log, status, error=None, email_queue=False):
	values = {"status": status, "attempts": log.attempts, "last_error": error}
	if email_queue is not False:
		values["email_queue"] = email_queue
	frappe.db.set_value(LOG, log.name, values)


def _short(exc: Exception) -> str:
	return f"{type(exc).__name__}: {str(exc)[:200]}"


# --------------------------------------------------------------------------- rendering
def render(log) -> tuple[str, str]:
	if log.reference_doctype == SESSION:
		session = frappe.get_doc(SESSION, log.reference_name)
		info = hrms_adapter.get_interview(session.interview)
		ctx = build_context(session, info, log.audience, json.loads(log.context_json or "{}"))
		if log.notification_type == ACCESS_LINK:
			from grid_interview.services import meeting

			# fresh single-use token on every (re)render; never stored in the log
			ctx["join_link"] = portal_url(f"/interview/join?t={meeting.create_link_token(session.name)}")
		subject = None
	else:
		from grid_interview.services import portal_emails

		subject, ctx = portal_emails.build(log)
	override = f"Grid Interview - {log.notification_type} - {log.audience}"
	if frappe.db.exists("Email Template", override):
		tpl = frappe.get_doc("Email Template", override)
		body = tpl.response_html if tpl.get("use_html") else tpl.response
		return frappe.render_template(tpl.subject, ctx), frappe.render_template(body or "", ctx)
	template = f"{TEMPLATE_DIR}/{log.notification_type}.html"
	subject = subject or _subject(log.notification_type, log.audience, ctx)
	return subject, frappe.render_template(template, ctx)


def build_context(session, info, audience: str, extra: dict) -> dict:
	"""All values are HTML-escaped here because the Jinja environment does not autoescape."""
	settings = get_settings()
	tz = frappe.utils.get_system_timezone()
	e = lambda v: escape_html(str(v)) if v not in (None, "") else ""  # noqa: E731
	ctx = {
		"audience": audience,
		"candidate_name": e(info.applicant_name),
		"job_title": e(info.job_title or info.designation),
		"interview_type": e(info.interview_type),
		"date": e(format_date(info.start, "d MMMM yyyy")),
		"weekday": e(format_date(info.start, "EEEE")),
		"start_time": e(format_time(info.start, "HH:mm")),
		"end_time": e(format_time(info.end, "HH:mm")),
		"timezone": e(tz),
		"duration_minutes": int((info.end - info.start).total_seconds() // 60),
		"instructions": e(session.instructions).replace("\n", "<br>"),
		"support_email": e(settings.support_email),
		"support_phone": e(settings.support_phone),
		"company": e(frappe.defaults.get_global_default("company")),
		"join_window_minutes": cint(settings.join_window_before_minutes),
		"candidate_message": e(extra.get("candidate_message")).replace("\n", "<br>"),
		"previous_date": "",
		"previous_start_time": "",
		"minutes_before": cint(extra.get("minutes_before")),
	}
	if extra.get("previous_start"):
		prev = get_datetime(extra["previous_start"])
		ctx["previous_date"] = e(format_date(prev, "d MMMM yyyy"))
		ctx["previous_start_time"] = e(format_time(prev, "HH:mm"))
	if audience == CANDIDATE:
		ctx["link"] = portal_url()
		ctx["password_link"] = portal_url("/interview-portal/password")
	else:
		ctx["link"] = get_url(f"/interview/room?session={session.name}")
		ctx["form_link"] = get_url_to_form(SESSION, session.name)
	return ctx


def portal_url(path: str = "/interview-portal") -> str:
	base = (get_settings().portal_base_url or "").strip().rstrip("/")
	return f"{base}{path}" if base else get_url(path)


# --------------------------------------------------------------------------- recipients
def recipients(info) -> list[tuple[str, str]]:
	"""(email, audience) pairs: the applicant plus every enabled interviewer."""
	out = []
	if info.applicant_email:
		out.append((info.applicant_email, CANDIDATE))
	for user in info.interviewers:
		row = frappe.db.get_value("User", user, ["email", "enabled"], as_dict=True)
		if row and row.enabled and row.email:
			out.append((row.email.lower(), INTERVIEWER))
	return out


def send_change_notices(session, notification_type: str, extra: dict | None = None, logical_suffix: str = ""):
	"""Scheduled / rescheduled / cancelled notices to every current recipient (idempotent)."""
	info = hrms_adapter.get_interview(session.interview)
	sent = []
	for email, audience in recipients(info):
		logical = f"{notification_type}:{session.name}:{logical_suffix}:{email}"
		if notify(notification_type, session.name, email, audience, logical, extra=extra):
			sent.append(email)
	return sent


# --------------------------------------------------------------------------- reminders
def hrms_reminders_enabled() -> bool:
	"""HRMS's own reminder (HR Settings) emails the same people; never send both."""
	if not hrms_adapter.is_available():
		return False
	return bool(cint(frappe.db.get_single_value("HR Settings", "send_interview_reminder")))


def send_due_reminders(now=None) -> int:
	from grid_interview.utils.settings import reminder_offsets

	now = now or now_datetime()
	offsets = reminder_offsets()
	if not offsets or not cint(get_settings().enable_email_notifications) or hrms_reminders_enabled():
		return 0
	horizon = add_to_date(now, minutes=max(offsets))
	sessions = frappe.get_all(
		SESSION,
		filters={"status": "Scheduled", "scheduled_start": ["between", [now, horizon]]},
		pluck="name",
	)
	count = 0
	for name in sessions:
		try:
			count += _remind_session(name, offsets, now)
		except Exception:
			frappe.log_error(title=f"grid_interview reminder failed for {name}")
	return count


def _remind_session(session_name: str, offsets: list[int], now) -> int:
	session = frappe.get_doc(SESSION, session_name)
	info = hrms_adapter.get_interview(session.interview)
	if info.is_cancelled or info.start <= now:
		return 0
	due = [o for o in offsets if info.start - timedelta(minutes=o) <= now]
	if not due:
		return 0
	latest = min(due)  # several reminders due at once (late scheduling, scheduler down): send one
	start_key = info.start.isoformat()
	count = 0
	for email, audience in recipients(info):
		for offset in due:
			logical = f"{REMINDER}:{session.name}:{offset}:{start_key}:{email}"
			due_at = info.start - timedelta(minutes=offset)
			if offset != latest:
				record_skipped(REMINDER, session.name, email, audience, logical,
							   _("Superseded by a later reminder"))
			elif _announced_since(session.name, email, due_at):
				record_skipped(REMINDER, session.name, email, audience, logical,
							   _("Not needed: the invitation or reschedule email was sent after this reminder was due"))
			elif notify(REMINDER, session.name, email, audience, logical, extra={"minutes_before": offset},
						scheduled_for=due_at):
				count += 1
	return count


def _announced_since(session_name: str, recipient: str, when) -> bool:
	"""The recipient got the scheduled / rescheduled email at or after `when`."""
	return bool(frappe.db.exists(LOG, {
		"reference_name": session_name,
		"recipient": recipient,
		"notification_type": ["in", [SCHEDULED, RESCHEDULED]],
		"status": ["in", ["Queued", "Sent"]],
		"creation": [">=", when],
	}))


# --------------------------------------------------------------------------- delivery status / retries
def sync_delivery_status(now=None) -> None:
	"""Mirror Email Queue status into the log and retry failures with exponential backoff."""
	now = now or now_datetime()
	settings = get_settings()
	max_attempts = cint(settings.max_email_attempts) or 3
	base = cint(settings.retry_base_seconds) or 60
	logs = frappe.get_all(
		LOG,
		filters={"status": "Queued"},
		fields=["name", "email_queue", "attempts", "modified"],
		order_by="modified asc",
		limit=500,
	)
	for row in logs:
		try:
			if row.email_queue:
				status = frappe.db.get_value("Email Queue", row.email_queue, "status")
				if status in ("Sent", "Partially Sent"):
					frappe.db.set_value(LOG, row.name, {"status": "Sent", "last_error": None})
					continue
				if status != "Error":
					continue  # Not Sent / Sending: Frappe is still working on it
				error = frappe.db.get_value("Email Queue", row.email_queue, "error")
				if cint(row.attempts) >= max_attempts:
					frappe.db.set_value(LOG, row.name, {"status": "Failed", "last_error": (error or "")[:300]})
					continue
			elif cint(row.attempts) >= max_attempts:
				frappe.db.set_value(LOG, row.name, "status", "Failed")
				continue
			wait = base * (2 ** max(cint(row.attempts) - 1, 0))
			if get_datetime(row.modified) > add_to_date(now, seconds=-wait):
				continue  # backoff not elapsed
			deliver(frappe.get_doc(LOG, row.name))
		except Exception:
			frappe.log_error(title=f"grid_interview delivery sync failed for {row.name}")
