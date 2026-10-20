"""Interview scheduling on top of the standard HRMS Interview.

* The Interview is created / changed as the calling user (no ignore_permissions), so HRMS
  permissions and validations (duplicate round, designation) always apply.
* Interviews stay draft while scheduled; HRMS only allows submitting Cleared / Rejected ones.
* HRMS's own `Interview.reschedule_interview` is NOT called because it sends its own plain email,
  which would duplicate ours. Its approach is mirrored instead: the time fields are set-only-once,
  so they are changed with db_set after an explicit write-permission check.
* Checks HRMS does not do: end after start, not in the past, interviewers have the Interviewer
  role, no second active interview for the same applicant + type, no overlapping bookings.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime

from grid_interview.exceptions import GridInterviewError
from grid_interview.integrations import hrms_adapter
from grid_interview.services import notifications
from grid_interview.setup.install import ADMIN_ROLE, HR_ROLE
from grid_interview.utils import audit
from grid_interview.utils.settings import get_settings

SESSION = "Grid Interview Video Session"
SCHEDULER_ROLES = {ADMIN_ROLE, HR_ROLE, "HR Manager", "HR User", "System Manager"}
MIN_DURATION = timedelta(minutes=5)
MAX_DURATION = timedelta(hours=8)
RESCHEDULABLE = ("Scheduled", "Expired", "No Show")


class SchedulingConflict(GridInterviewError):
	pass


def can_schedule(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(SCHEDULER_ROLES.intersection(frappe.get_roles(user)))


def ensure_can_schedule():
	if not can_schedule():
		raise frappe.PermissionError(_("You are not allowed to schedule interviews."))


# --------------------------------------------------------------------------- validation helpers
def validate_window(scheduled_on, from_time, to_time):
	start = get_datetime(f"{getdate(scheduled_on)} {from_time}")
	end = get_datetime(f"{getdate(scheduled_on)} {to_time}")
	if end <= start:
		frappe.throw(_("End time must be after start time."))
	validate_duration(start, end)
	if start <= now_datetime():
		frappe.throw(_("The interview must start in the future."))
	return start, end


def validate_duration(start, end):
	"""Also applied to Interviews made in the HRMS form, which only checks the fields are filled."""
	if end - start < MIN_DURATION:
		frappe.throw(_("An interview must be at least 5 minutes long."))
	if end - start > MAX_DURATION:
		frappe.throw(_("An interview cannot be longer than 8 hours."))


def validate_interviewers(interviewers) -> list[str]:
	if isinstance(interviewers, str):
		interviewers = frappe.parse_json(interviewers) if interviewers.strip().startswith("[") else interviewers.split(",")
	users = list(dict.fromkeys(u.strip() for u in (interviewers or []) if u and u.strip()))
	if not users:
		frappe.throw(_("Select at least one interviewer."))
	for user in users:
		row = frappe.db.get_value("User", user, ["enabled", "user_type"], as_dict=True)
		if not row or not row.enabled or row.user_type != "System User":
			frappe.throw(_("{0} is not an active system user.").format(frappe.bold(user)))
		if "Interviewer" not in frappe.get_roles(user):
			frappe.throw(_("{0} does not have the Interviewer role.").format(frappe.bold(user)))
	return users


def find_conflicts(start, end, applicant: str, interviewers: list[str], exclude: str | None = None) -> list[str]:
	"""Active interviews that overlap [start, end) for the same applicant or any interviewer."""
	day = getdate(start)
	rows = frappe.get_all(
		"Interview",
		filters={
			"scheduled_on": ["between", [add_days(day, -1), add_days(day, 1)]],
			"docstatus": ["<", 2],
			"status": ["!=", "Cancelled"],
			"name": ["!=", exclude or ""],
		},
		fields=["name", "job_applicant", "scheduled_on", "from_time", "to_time"],
	)
	conflicts = []
	for row in rows:
		other_start, other_end = hrms_adapter.interview_window(row.scheduled_on, row.from_time, row.to_time)
		if not (other_start < end and start < other_end):
			continue
		if row.job_applicant == applicant or set(interviewers) & set(hrms_adapter.get_interviewers(row.name)):
			conflicts.append(row.name)
	return conflicts


def _raise_conflicts(conflicts):
	if conflicts:
		raise SchedulingConflict(
			_("The candidate or an interviewer is already booked at this time: {0}. Choose another time or confirm the overlap.").format(
				", ".join(conflicts)
			)
		)


# --------------------------------------------------------------------------- operations
def schedule_interview(
	job_applicant: str,
	interview_type: str,
	scheduled_on,
	from_time,
	to_time,
	interviewers,
	instructions: str | None = None,
	recording_eligible=0,
	allow_overlap=0,
) -> dict:
	hrms_adapter.ensure_available()
	ensure_can_schedule()
	start, end = validate_window(scheduled_on, from_time, to_time)
	applicant = hrms_adapter.get_applicant(job_applicant)
	if not applicant:
		frappe.throw(_("Job Applicant {0} not found.").format(job_applicant))
	if applicant.status in ("Rejected", "Accepted"):
		frappe.throw(_("Job Applicant {0} is {1} and cannot be scheduled.").format(job_applicant, applicant.status))
	if not (applicant.email_id or "").strip():
		frappe.throw(_("Job Applicant {0} has no email address.").format(job_applicant))
	if not frappe.db.exists("Interview Type", interview_type):
		frappe.throw(_("Interview Type {0} not found.").format(interview_type))
	users = validate_interviewers(interviewers)

	# Same applicant + round: an undecided interview is rescheduled, not duplicated. A submitted one
	# blocks too, because HRMS allows one submitted Interview per Interview Type and would refuse the
	# new one anyway; say so plainly. A decided but unsubmitted draft does not block (HRMS allows it).
	active = frappe.db.exists(
		"Interview",
		{"job_applicant": job_applicant, "interview_type": interview_type, "docstatus": 0,
		 "status": ["in", ["Pending", "Under Review"]]},
	)
	if active:
		frappe.throw(_("{0} already has an active {1} interview: {2}. Reschedule it instead.").format(
			job_applicant, interview_type, active))
	submitted = frappe.db.get_value(
		"Interview", {"job_applicant": job_applicant, "interview_type": interview_type, "docstatus": 1},
		["name", "status"], as_dict=True)
	if submitted:
		frappe.throw(_("{0} already completed {1} ({2}: {3}). HRMS allows one submitted interview "
					   "per Interview Type, so schedule the next round with another Interview Type.").format(
			job_applicant, interview_type, submitted.name, _(submitted.status)))
	if not cint(allow_overlap):
		_raise_conflicts(find_conflicts(start, end, job_applicant, users))

	interview = frappe.get_doc(
		{
			"doctype": "Interview",
			"interview_type": interview_type,
			"job_applicant": job_applicant,
			"scheduled_on": getdate(scheduled_on),
			"from_time": str(from_time),
			"to_time": str(to_time),
			"status": "Pending",
			"interview_details": [{"interviewer": u} for u in users],
		}
	).insert()  # as the current user: HRMS permissions + validations apply
	session = create_session(interview.name, instructions, recording_eligible)
	provision_candidate(applicant.email_id, applicant.applicant_name)
	audit.log("interview.scheduled", SESSION, session.name, interview=interview.name, start=start)
	notifications.send_change_notices(session, notifications.SCHEDULED)
	return {"interview": interview.name, "video_session": session.name}


def provision_candidate(email: str, full_name: str) -> str | None:
	"""Invitation: make sure the applicant has a candidate portal account (no password is set;
	the scheduled email links to the portal's set-password page). Never blocks scheduling."""
	if not cint(get_settings().enable_candidate_portal):
		return None
	from grid_interview.services import candidates

	frappe.db.savepoint("gi_provision")
	try:
		return candidates.ensure_candidate_account(email, full_name, "Invitation")
	except Exception:
		frappe.db.rollback(save_point="gi_provision")
		frappe.log_error(title="grid_interview candidate account provisioning failed")
		return None


def create_session(interview_name: str, instructions=None, recording_eligible=0):
	info = hrms_adapter.get_interview(interview_name)
	return frappe.get_doc(
		{
			"doctype": SESSION,
			"interview": interview_name,
			"job_applicant": info.job_applicant,
			"scheduled_start": info.start,
			"scheduled_end": info.end,
			"provider": get_settings().video_provider,
			"instructions": (instructions or "").strip()[:2000],
			"recording_eligible": cint(recording_eligible),
			"recording_status": "Not Requested",
		}
	).insert(ignore_permissions=True)


def attach_existing(interview: str, instructions=None, recording_eligible=0) -> dict:
	"""Manage an Interview that was created directly in HRMS Desk."""
	hrms_adapter.ensure_available()
	ensure_can_schedule()
	frappe.has_permission("Interview", "read", interview, throw=True)
	info = hrms_adapter.get_interview(interview)
	if info.is_cancelled or info.docstatus != 0:
		frappe.throw(_("Only pending (draft) interviews can be managed."))
	if info.start <= now_datetime():
		frappe.throw(_("This interview is in the past."))
	validate_duration(info.start, info.end)
	if frappe.db.exists(SESSION, {"interview": interview}):
		frappe.throw(_("This interview is already managed by Grid Interview."))
	session = create_session(interview, instructions, recording_eligible)
	provision_candidate(info.applicant_email, info.applicant_name)
	audit.log("interview.attached", SESSION, session.name, interview=interview)
	notifications.send_change_notices(session, notifications.SCHEDULED)
	return {"interview": interview, "video_session": session.name}


def manage_desk_interviews(now=None) -> list[str]:
	"""Pick up Interviews created in the standard HRMS form and manage them like Scheduler ones
	(session, candidate account, invitations). Runs from the 5-minute cycle instead of an Interview
	doc_event, so HRMS itself is never hooked. Only draft, not cancelled, future interviews without a
	session; switched off by Grid Interview Settings → Auto-manage Interviews Created in HRMS."""
	if not hrms_adapter.is_available() or not cint(get_settings().auto_manage_desk_interviews):
		return []
	now = now or now_datetime()
	candidates = frappe.get_all(
		"Interview",
		filters={"docstatus": 0, "status": ["!=", "Cancelled"], "scheduled_on": [">=", getdate(now)]},
		pluck="name",
		limit=200,
	)
	managed = set(frappe.get_all(SESSION, filters={"interview": ["in", candidates]}, pluck="interview")) \
		if candidates else set()
	done = []
	for name in candidates:
		if name in managed:
			continue
		frappe.db.savepoint("gi_manage_desk")
		try:
			info = hrms_adapter.get_interview(name)
			if info.is_cancelled or info.start <= now:
				continue
			if not MIN_DURATION <= info.end - info.start <= MAX_DURATION:
				# not managed until HR fixes the times; reported once a day, not every cycle
				if not frappe.cache.get_value(f"gi_bad_duration:{name}"):
					frappe.cache.set_value(f"gi_bad_duration:{name}", 1, expires_in_sec=86400)
					frappe.log_error(title=f"grid_interview: {name} not managed, it must be 5 minutes to 8 hours long",
									 message=f"{info.start} to {info.end}. Fix From / To Time in the Interview.")
				continue
			session = create_session(name)
			provision_candidate(info.applicant_email, info.applicant_name)
			audit.log("interview.auto_managed", SESSION, session.name, interview=name)
			notifications.send_change_notices(session, notifications.SCHEDULED)
			done.append(name)
		except Exception:
			frappe.db.rollback(save_point="gi_manage_desk")
			frappe.log_error(title=f"grid_interview auto-manage failed for {name}")
	return done


def reschedule(session_name: str, scheduled_on, from_time, to_time, reason=None, candidate_message=None,
			   allow_overlap=0) -> dict:
	hrms_adapter.ensure_available()
	ensure_can_schedule()
	session = frappe.get_doc(SESSION, session_name)
	if session.status not in RESCHEDULABLE:
		frappe.throw(_("A {0} interview cannot be rescheduled.").format(session.status.lower()))
	interview = frappe.get_doc("Interview", session.interview)
	if interview.docstatus != 0 or interview.status == "Cancelled":
		frappe.throw(_("Only pending interviews can be rescheduled."))
	start, end = validate_window(scheduled_on, from_time, to_time)
	old_start = session.scheduled_start
	if get_datetime(old_start) == start and get_datetime(session.scheduled_end) == end:
		return {"changed": False}
	interviewers = [d.interviewer for d in interview.interview_details]
	if not cint(allow_overlap):
		_raise_conflicts(find_conflicts(start, end, interview.job_applicant, interviewers, exclude=interview.name))

	# HRMS marks the time fields set-only-once; its own Interview.reschedule_interview changes them
	# with db_set after a write-permission check. Do the same (minus HRMS's plain email, ours replaces it).
	frappe.has_permission("Interview", "write", interview, throw=True)
	interview.db_set({"scheduled_on": getdate(scheduled_on), "from_time": str(from_time), "to_time": str(to_time)})
	interview.notify_update()
	interview.add_comment(
		"Info", _("Rescheduled via Grid Interview from {0} to {1}").format(old_start, start)
	)

	session.scheduled_start, session.scheduled_end = start, end
	session.status = "Scheduled"
	session.save(ignore_permissions=True)
	revoke_access(session.name, "Interview rescheduled")
	audit.log("interview.rescheduled", SESSION, session.name, interview=interview.name,
			  previous_start=old_start, start=start, reason=reason)
	notifications.send_change_notices(
		session,
		notifications.RESCHEDULED,
		extra={"previous_start": old_start, "candidate_message": candidate_message},
		logical_suffix=start.isoformat(),
	)
	return {"changed": True}


def cancel(session_name: str, reason=None, candidate_message=None) -> dict:
	hrms_adapter.ensure_available()
	ensure_can_schedule()
	session = frappe.get_doc(SESSION, session_name)
	if session.status == "Cancelled":
		return {"changed": False}
	if session.status in ("Completed", "In Progress"):
		frappe.throw(_("A {0} interview cannot be cancelled.").format(session.status.lower()))
	interview = frappe.get_doc("Interview", session.interview)
	if interview.docstatus == 1:
		frappe.throw(_("A completed (submitted) interview cannot be cancelled."))
	if interview.docstatus == 0 and interview.status != "Cancelled":
		interview.status = "Cancelled"
		interview.save()  # as the current user
	_mark_cancelled(session)
	audit.log("interview.cancelled", SESSION, session.name, interview=interview.name, reason=reason)
	notifications.send_change_notices(session, notifications.CANCELLED, extra={"candidate_message": candidate_message})
	return {"changed": True}


def resend_invitation(session_name: str) -> list[str]:
	ensure_can_schedule()
	session = frappe.get_doc(SESSION, session_name)
	if session.status != "Scheduled":
		frappe.throw(_("Invitations can only be resent for scheduled interviews."))
	# minute bucket: an explicit resend is allowed, an accidental double click is not
	bucket = now_datetime().strftime("%Y%m%d%H%M")
	sent = notifications.send_change_notices(session, notifications.SCHEDULED, logical_suffix=f"resend-{bucket}")
	audit.log("interview.invitation_resent", SESSION, session.name, recipients=len(sent))
	return sent


def _mark_cancelled(session):
	session.status = "Cancelled"
	session.cancelled_on = now_datetime()
	session.flags.ignore_links = True  # the HRMS Interview may already be deleted
	session.save(ignore_permissions=True)
	revoke_access(session.name, "Interview cancelled")


def revoke_access(session_name: str, reason: str) -> int:
	rows = frappe.get_all(
		"Grid Interview Access Session", filters={"video_session": session_name, "revoked": 0}, pluck="name"
	)
	for name in rows:
		frappe.db.set_value(
			"Grid Interview Access Session",
			name,
			{"revoked": 1, "revoked_on": now_datetime(), "revoke_reason": reason[:140]},
		)
	if rows:
		audit.log("access.revoked", SESSION, session_name, count=len(rows), reason=reason)
	return len(rows)


# --------------------------------------------------------------------------- reconcile (scheduler)
def reconcile_sessions(now=None) -> None:
	"""Pick up changes made directly in HRMS Desk (edit, HRMS 'Reschedule', discard, delete)."""
	if not hrms_adapter.is_available():
		return
	now = now or now_datetime()
	sessions = frappe.get_all(
		SESSION,
		filters={"status": "Scheduled", "scheduled_start": [">=", add_days(now, -2)]},
		pluck="name",
	)
	for name in sessions:
		try:
			_reconcile(name, now)
		except Exception:
			frappe.log_error(title=f"grid_interview reconcile failed for {name}")


def _reconcile(name: str, now):
	session = frappe.get_doc(SESSION, name)
	try:
		info = hrms_adapter.get_interview(session.interview)
	except frappe.DoesNotExistError:
		_mark_cancelled(session)
		audit.log("interview.deleted_outside", SESSION, name, interview=session.interview)
		return
	if info.is_cancelled:
		_mark_cancelled(session)
		audit.log("interview.cancelled_outside", SESSION, name, interview=info.name)
		notifications.send_change_notices(session, notifications.CANCELLED)
		return
	if info.start != get_datetime(session.scheduled_start) or info.end != get_datetime(session.scheduled_end):
		previous = session.scheduled_start
		session.scheduled_start, session.scheduled_end = info.start, info.end
		session.save(ignore_permissions=True)
		revoke_access(name, "Interview time changed")
		audit.log("interview.rescheduled_outside", SESSION, name, interview=info.name,
				  previous_start=previous, start=info.start)
		if info.start > now:
			notifications.send_change_notices(
				session, notifications.RESCHEDULED, extra={"previous_start": previous},
				logical_suffix=info.start.isoformat(),
			)
		return
	if info.start > now:
		# interviewers added in Desk after scheduling also receive the invitation (idempotent)
		notifications.send_change_notices(session, notifications.SCHEDULED)
	# sessions past their window are closed by meeting.finalize_sessions (No Show / Expired / Completed)


def run_cycle():
	"""Scheduler entry point (every 5 minutes): manage new HRMS-form interviews, reconcile Desk
	changes, close finished meetings, send due reminders, sync Email Queue status."""
	from grid_interview.services import jobs, meeting

	for step in (manage_desk_interviews, reconcile_sessions, meeting.finalize_sessions, notifications.send_due_reminders,
				 notifications.sync_delivery_status, jobs.retry_due):
		try:
			step()
		except Exception:
			frappe.log_error(title=f"grid_interview cycle step failed: {getattr(step, '__name__', step)}")
