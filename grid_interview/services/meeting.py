"""Meeting access and lifecycle for Grid Interview Video Sessions.

Who may join (decided only here, on the server):
  * the candidate: a signed-in candidate whose applicant email matches the session, or a
    browser holding a valid, bound join link (optional feature, see `redeem_link`);
  * interviewers assigned on the HRMS Interview (moderators);
  * Grid Interview Administrators / HR users (moderators).
When: inside the join window (start - N min .. end + M min) of a non-terminal session.

Status rules: a session becomes In Progress only when BOTH a candidate and an interviewer
have joined; opening the join page or joining alone never completes an interview. Completed
requires both to have joined and the meeting to be ended by a moderator (or to have gone
quiet after the scheduled end). An interviewer-only meeting after the window is a No Show;
no participants is Expired.
"""

from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from grid_interview.exceptions import AccessDenied, InvalidAccessToken, OutsideJoinWindow
from grid_interview.integrations import hrms_adapter
from grid_interview.setup.install import ADMIN_ROLE, HR_ROLE
from grid_interview.utils import audit
from grid_interview.utils.security import hash_token, new_token
from grid_interview.utils.settings import get_settings
from grid_interview.video import Participant, get_provider

SESSION = "Grid Interview Video Session"
ACCESS = "Grid Interview Access Session"
COOKIE = "gi_access"
TERMINAL = ("Completed", "No Show", "Cancelled", "Expired")
HR_ROLES = {ADMIN_ROLE, HR_ROLE}
HEARTBEAT_SECONDS = 30
MAX_LINK_FAILURES = 5


# --------------------------------------------------------------------------- window
def join_window(session) -> tuple:
	s = get_settings()
	start, end = get_datetime(session.scheduled_start), get_datetime(session.scheduled_end)
	return (start - timedelta(minutes=cint(s.join_window_before_minutes)),
			end + timedelta(minutes=cint(s.join_window_after_end_minutes)))


def ensure_joinable(session, now=None):
	now = now or now_datetime()
	if session.status in TERMINAL:
		raise OutsideJoinWindow(_("This interview is no longer available ({0}).").format(_(session.status)))
	opens, closes = join_window(session)
	if now < opens:
		raise OutsideJoinWindow(
			_("You can join from {0}.").format(frappe.utils.format_datetime(opens, "d MMM yyyy, HH:mm"))
		)
	if now > closes:
		raise OutsideJoinWindow(_("The joining time for this interview has passed."))
	return opens, closes


# --------------------------------------------------------------------------- who is joining
def resolve_participant(session, access=None) -> Participant:
	if access is not None:
		if access.video_session != session.name:
			raise AccessDenied(_("You are not invited to this interview."))
		info = hrms_adapter.get_interview(session.interview)
		return Participant("Candidate", f"access:{access.name}", info.applicant_name or _("Candidate"),
						   access.candidate_email, False)

	user = frappe.session.user
	if user in (None, "Guest"):
		raise AccessDenied(_("Please sign in to join the interview."))
	full_name = frappe.utils.get_fullname(user)
	email = frappe.db.get_value("User", user, "email")
	if hrms_adapter.is_interviewer(user, session.interview):
		return Participant("Interviewer", user, full_name, email, True)
	if user == "Administrator" or HR_ROLES.intersection(frappe.get_roles(user)):
		return Participant("HR", user, full_name, email, True)

	from grid_interview.services import candidates, portal

	if candidates.is_candidate(user):
		profile = candidates.require_candidate()  # includes the CSRF check for POSTs
		portal.own_session(profile, session.name)  # PermissionError if not theirs
		return Participant("Candidate", user, profile.full_name or full_name, profile.email, False)
	raise AccessDenied(_("You are not a participant of this interview."))


def check_request_origin(access=None):
	"""CSRF defence for meeting POSTs.
	Signed-in users: the session CSRF token is required (also for website users, for whom
	Frappe itself skips the check). Link users (Guest + bound cookie): a custom header is
	required, which a cross-site form or image cannot send without a CORS preflight."""
	request = getattr(frappe.local, "request", None)
	if not request or request.method != "POST":
		return
	if access is not None:
		if frappe.get_request_header("X-Grid-Interview") != "1":
			raise frappe.CSRFTokenError(_("Invalid request."))
		return
	from grid_interview.services.candidates import check_csrf

	check_csrf()


# --------------------------------------------------------------------------- room page details
def room_details(session_name: str, access=None) -> dict:
	"""What the room page shows. Candidates get only their own interview basics; moderators
	also get the candidate, round rubric and links to the HRMS records."""
	session = frappe.get_doc(SESSION, session_name)
	participant = resolve_participant(session, access)
	info = hrms_adapter.get_interview(session.interview)
	opens, closes = join_window(session)
	settings = get_settings()
	data = {
		"session": session.name,
		"participant_kind": participant.kind,
		"moderator": participant.moderator,
		"display_name": participant.display_name,
		"job_title": info.job_title or info.designation,
		"round": info.interview_type,
		"start": str(session.scheduled_start),
		"end": str(session.scheduled_end),
		"opens": str(opens),
		"closes": str(closes),
		"status": session.status,
		"instructions": session.instructions,
		"may_be_recorded": bool(cint(session.recording_eligible) and cint(settings.enable_recording)),
		"provider": session.provider or settings.video_provider,
		# pre-formatted in the site time zone so every browser shows the same, correct time
		"start_label": frappe.utils.format_datetime(session.scheduled_start, "EEE d MMM yyyy, HH:mm"),
		"end_label": frappe.utils.format_datetime(session.scheduled_end, "HH:mm"),
		"opens_label": frappe.utils.format_datetime(opens, "EEE d MMM, HH:mm"),
		"closes_label": frappe.utils.format_datetime(closes, "EEE d MMM, HH:mm"),
		"timezone": frappe.utils.get_system_timezone(),
	}
	if data["may_be_recorded"]:
		data["consent_text"] = settings.consent_text or ""  # admin-authored, sanitized by Frappe on save
		data["policy_version"] = settings.consent_policy_version or "v1"
		data["external_processing_offered"] = bool(
			cint(settings.enable_external_processing)
			and "OpenAI Compatible" in (settings.transcription_provider, settings.ai_provider)
		)
	if participant.moderator:
		data.update(
			{
				"candidate_name": info.applicant_name,
				"job_applicant": info.job_applicant,
				"interview": info.name,
				"interviewers": [frappe.utils.get_fullname(u) for u in info.interviewers],
				"expected_skills": hrms_adapter.get_expected_skills(info.interview_type),
				"candidate_joined": bool(session.candidate_joined),
				"interviewer_joined": bool(session.interviewer_joined),
			}
		)
	return data


# --------------------------------------------------------------------------- join / presence
def join(session_name: str, access=None) -> dict:
	check_request_origin(access)
	session = frappe.get_doc(SESSION, session_name, for_update=True)
	participant = resolve_participant(session, access)
	try:
		_opens, closes = ensure_joinable(session)
	except OutsideJoinWindow:
		audit.log("meeting.join_refused", SESSION, session.name, result="Denied", kind=participant.kind)
		raise
	provider = get_provider(session.provider or None)
	# The token may carry Jitsi's recording feature only for eligible sessions; actually starting
	# a recording still needs server approval with every participant's consent
	# (grid_interview.services.recording.start), and unapproved Jibri files are discarded.
	recording_allowed = bool(cint(session.recording_eligible) and cint(get_settings().enable_recording))
	config = provider.join_config(session, participant, closes, recording_allowed=recording_allowed)
	_register_join(session, participant)
	if access is not None:
		frappe.db.set_value(ACCESS, access.name, {
			"last_access_on": now_datetime(),
			"access_count": cint(access.access_count) + 1,
		})
	audit.log("meeting.joined", SESSION, session.name, kind=participant.kind, provider=provider.name)
	config.update(
		{
			"session": session.name,
			"participant_kind": participant.kind,
			"heartbeat_seconds": HEARTBEAT_SECONDS,
			"scheduled_end": str(session.scheduled_end),
			"status": session.status,
		}
	)
	return config


def _register_join(session, participant: Participant):
	user = participant.identity if not participant.identity.startswith("access:") else None
	identity_note = participant.identity if user is None else None
	already = any(
		(e.user == user if user else e.details == identity_note) and e.event_type in ("Joined", "Reconnected")
		for e in session.participant_events
	)
	session.add_event("Reconnected" if already else "Joined", participant.kind, user=user, details=identity_note)
	if participant.kind == "Candidate":
		session.candidate_joined = 1
	else:
		session.interviewer_joined = 1
	if session.status == "Scheduled" and session.candidate_joined and session.interviewer_joined:
		session.status = "In Progress"
		session.actual_start = now_datetime()
		audit.log("meeting.started", SESSION, session.name)
	session.save(ignore_permissions=True)
	_touch(session.name, participant.identity)
	_stop_recording_for_unconsented(session, participant)


def _stop_recording_for_unconsented(session, participant):
	"""Someone joined a live recording without a current consent: stop recording at once.
	The moderator's heartbeat sees recording_live = False and stops the provider recording."""
	from grid_interview.services import consent, recording

	live = recording.live_recording(session.name)
	if live and not consent.current(session.name, participant.identity, consent.RECORDING):
		frappe.db.set_value(recording.RECORDING, live, {"status": "Finalizing", "ended_on": now_datetime()})
		frappe.db.set_value(SESSION, session.name, "recording_status", "Stopped")
		audit.log("recording.stopped_unconsented_participant", recording.RECORDING, live, kind=participant.kind)


def _presence_key(session_name: str, identity: str) -> str:
	return f"grid_interview:presence:{session_name}:{identity}"


def _touch(session_name: str, identity: str):
	timeout = cint(get_settings().presence_timeout_seconds or 120)
	frappe.cache.set_value(_presence_key(session_name, identity), now_datetime().isoformat(), expires_in_sec=timeout)


def is_present(session_name: str, identity: str) -> bool:
	return bool(frappe.cache.get_value(_presence_key(session_name, identity)))


def heartbeat(session_name: str, access=None) -> dict:
	"""Cheap keep-alive (cache only, no DB write). Also tells the client if the meeting ended."""
	check_request_origin(access)
	session = frappe.get_doc(SESSION, session_name)
	participant = resolve_participant(session, access)
	from grid_interview.services import recording

	if session.status in TERMINAL:
		return {"status": session.status, "active": False, **recording.room_state(session)}
	_touch(session.name, participant.identity)
	return {"status": session.status, "active": True, **recording.room_state(session)}


def leave(session_name: str, access=None) -> dict:
	check_request_origin(access)
	session = frappe.get_doc(SESSION, session_name, for_update=True)
	participant = resolve_participant(session, access)
	user = None if participant.identity.startswith("access:") else participant.identity
	session.add_event("Left", participant.kind, user=user, details=None if user else participant.identity)
	session.save(ignore_permissions=True)
	frappe.cache.delete_value(_presence_key(session.name, participant.identity))
	return {"status": session.status}


def end_meeting(session_name: str) -> dict:
	"""Moderator ends the interview. Completed only if both sides actually joined."""
	check_request_origin()
	session = frappe.get_doc(SESSION, session_name, for_update=True)
	participant = resolve_participant(session)
	if not participant.moderator:
		raise AccessDenied(_("Only the interviewer can end the interview."))
	if session.status in TERMINAL:
		return {"status": session.status}
	now = now_datetime()
	if session.candidate_joined and session.interviewer_joined:
		session.status = "Completed"
	elif now >= get_datetime(session.scheduled_start) and session.interviewer_joined:
		session.status = "No Show"
	else:
		frappe.throw(_("The candidate has not joined yet. Wait, or cancel / reschedule the interview."))
	session.actual_end = now
	from grid_interview.services import recording

	live = recording.live_recording(session.name)
	if live:  # ending the meeting stops the recording; Jibri then uploads the file
		frappe.db.set_value(recording.RECORDING, live, {"status": "Finalizing", "ended_on": now})
		session.recording_status = "Stopped"
	session.add_event("Left", participant.kind, user=participant.identity, details="ended the interview")
	session.save(ignore_permissions=True)
	_revoke_links(session.name, "Interview ended")
	audit.log("meeting.ended", SESSION, session.name, status=session.status)
	return {"status": session.status}


def finalize_sessions(now=None) -> None:
	"""Scheduler: close sessions whose window has passed, record silent disconnects."""
	now = now or now_datetime()
	after = timedelta(minutes=cint(get_settings().join_window_after_end_minutes))
	for name in frappe.get_all(SESSION, filters={"status": "In Progress"}, pluck="name"):
		try:
			_finalize_in_progress(name, now, after)
		except Exception:
			frappe.log_error(title=f"grid_interview finalize failed for {name}")
	for name in frappe.get_all(
		SESSION, filters={"status": "Scheduled", "scheduled_end": ["<", now - after]}, pluck="name"
	):
		session = frappe.get_doc(SESSION, name, for_update=True)
		if session.interviewer_joined and not session.candidate_joined:
			session.status = "No Show"
		else:
			session.status = "Expired"  # nobody joined, or the interviewer never joined
		session.save(ignore_permissions=True)
		_revoke_links(name, f"Interview {session.status.lower()}")
		audit.log(f"meeting.{session.status.lower().replace(' ', '_')}", SESSION, name)


def _finalize_in_progress(name, now, after):
	session = frappe.get_doc(SESSION, name, for_update=True)
	identities = {e.user or e.details for e in session.participant_events if e.event_type in ("Joined", "Reconnected")}
	last = {}
	for e in session.participant_events:
		last[e.user or e.details] = e
	changed = False
	for identity in identities:
		if not is_present(name, identity) and last[identity].event_type not in ("Left", "Disconnected"):
			e = last[identity]
			session.add_event("Disconnected", e.participant_type, user=e.user, details=e.details)
			changed = True
	nobody_here = not any(is_present(name, i) for i in identities)
	if nobody_here and now > get_datetime(session.scheduled_end) + after:
		session.status = "Completed"  # both had joined (In Progress), the window is over
		session.actual_end = now
		changed = True
		_revoke_links(name, "Interview ended")
		audit.log("meeting.completed_by_timeout", SESSION, name)
	if changed:
		session.save(ignore_permissions=True)


# --------------------------------------------------------------------------- passwordless join links
def link_expiry(session):
	_opens, closes = join_window(session)
	hours = cint(get_settings().access_link_validity_hours)
	return min(closes, now_datetime() + timedelta(hours=hours)) if hours else closes


def create_link_token(session_name: str) -> str:
	"""Issue a new candidate join link token (previous unused links are revoked).
	Called while rendering the access-link email, so the raw token is never stored."""
	if not cint(get_settings().allow_link_join):
		frappe.throw(_("Passwordless join links are disabled in Grid Interview Settings."))
	session = frappe.get_doc(SESSION, session_name)
	info = hrms_adapter.get_interview(session.interview)
	_revoke_links(session_name, "Replaced by a new link")
	raw, hashed = new_token()
	frappe.get_doc(
		{
			"doctype": ACCESS,
			"video_session": session_name,
			"candidate_email": info.applicant_email,
			"candidate_user": frappe.db.get_value("User", {"email": info.applicant_email}, "name"),
			"token_hash": hashed,
			"issued_on": now_datetime(),
			"expires_on": link_expiry(session),
			"allowed_actions": "join",
		}
	).insert(ignore_permissions=True)
	return raw


def issue_join_link(session_name: str) -> None:
	"""HR action: email the candidate a join link (token created at render time)."""
	from grid_interview.services import notifications, scheduling

	scheduling.ensure_can_schedule()
	if not cint(get_settings().allow_link_join):
		frappe.throw(_("Passwordless join links are disabled in Grid Interview Settings."))
	session = frappe.get_doc(SESSION, session_name)
	if session.status != "Scheduled":
		frappe.throw(_("Join links can only be sent for scheduled interviews."))
	info = hrms_adapter.get_interview(session.interview)
	notifications.notify(
		notifications.ACCESS_LINK, session.name, info.applicant_email, notifications.CANDIDATE,
		f"access_link:{session.name}:{now_datetime().isoformat()}",
	)
	audit.log("access.link_requested", SESSION, session.name)


def _revoke_links(session_name: str, reason: str):
	for name in frappe.get_all(ACCESS, filters={"video_session": session_name, "revoked": 0}, pluck="name"):
		frappe.db.set_value(ACCESS, name, {"revoked": 1, "revoked_on": now_datetime(), "revoke_reason": reason[:140]})


def redeem_link(raw: str, cookie_value: str | None = None):
	"""Validate a join link. First use binds it to this browser (returns a new binding secret
	for an HttpOnly cookie); later uses must present that cookie (reconnects work, a forwarded
	or replayed link does not). Returns (access_doc, new_binding_secret_or_None)."""
	generic = _("This join link is invalid, has expired or was opened in another browser.")
	if not cint(get_settings().allow_link_join) or not raw:
		raise InvalidAccessToken(generic)
	name = frappe.db.get_value(ACCESS, {"token_hash": hash_token(raw)}, "name")
	if not name:
		audit.log("access.link_rejected", actor="Guest", result="Denied", reason="unknown")
		raise InvalidAccessToken(generic)
	access = frappe.get_doc(ACCESS, name, for_update=True)
	session_status = frappe.db.get_value(SESSION, access.video_session, "status")
	if access.revoked or get_datetime(access.expires_on) < now_datetime() or session_status in TERMINAL:
		audit.log("access.link_rejected", ACCESS, name, actor="Guest", result="Denied", reason="expired or revoked")
		raise InvalidAccessToken(generic)
	if not access.redeemed_on:
		secret, binding = new_token()
		access.redeemed_on = now_datetime()
		access.binding_hash = binding
		access.save(ignore_permissions=True)
		audit.log("access.link_redeemed", ACCESS, name, actor="Guest")
		return access, secret
	presented = (cookie_value or "").partition(".")
	if presented[0] == access.name and hash_token(presented[2]) == access.binding_hash:
		access.reconnect_count = cint(access.reconnect_count) + 1
		access.save(ignore_permissions=True)
		return access, None
	access.failed_attempts = cint(access.failed_attempts) + 1
	if access.failed_attempts >= MAX_LINK_FAILURES:
		access.revoked, access.revoked_on, access.revoke_reason = 1, now_datetime(), "Too many failed attempts"
	access.save(ignore_permissions=True)
	audit.log("access.link_rejected", ACCESS, name, actor="Guest", result="Denied", reason="other browser")
	raise InvalidAccessToken(generic)


def access_from_cookie(session_name: str | None = None):
	"""The bound, valid access session of this browser (or None)."""
	value = frappe.request.cookies.get(COOKIE) if getattr(frappe, "request", None) else None
	if not value or "." not in value or not cint(get_settings().allow_link_join):
		return None
	name, _sep, secret = value.partition(".")
	row = frappe.db.get_value(ACCESS, name, ["name", "binding_hash", "revoked", "expires_on", "video_session",
											 "candidate_email", "access_count"], as_dict=True)
	if (
		not row or row.revoked or not row.binding_hash or hash_token(secret) != row.binding_hash
		or get_datetime(row.expires_on) < now_datetime()
		or (session_name and row.video_session != session_name)
	):
		return None
	return row


def set_access_cookie(access, secret: str):
	max_age = max(60, int((get_datetime(access.expires_on) - now_datetime()).total_seconds()))
	frappe.local.cookie_manager.set_cookie(
		COOKIE, f"{access.name}.{secret}", max_age=max_age, httponly=True, samesite="Lax"
	)
