"""Recording / external-processing consent.

Each participant gives or withdraws consent themselves in the interview room; every change is a
new Grid Interview Consent row (history is never overwritten) carrying the policy version.
A consent for an older policy version does not count. Recording may start only when every
participant who has joined the session (candidate included) has a current Recording consent.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from grid_interview.utils import audit
from grid_interview.utils.security import client_fingerprint
from grid_interview.utils.settings import get_settings

CONSENT = "Grid Interview Consent"
RECORDING = "Recording"
EXTERNAL = "External Processing"
TYPES = (RECORDING, EXTERNAL)


def _identity(participant) -> str:
	return participant.identity  # user name, or "access:<access session>" for link joins


def current(session_name: str, identity: str, consent_type: str) -> bool:
	row = frappe.get_all(
		CONSENT,
		filters={"video_session": session_name, "participant_name": identity, "consent_type": consent_type},
		fields=["status", "policy_version"],
		order_by="creation desc",
		limit=1,
	)
	return bool(row) and row[0].status == "Granted" and row[0].policy_version == (get_settings().consent_policy_version or "v1")


def record(session, participant, consent_type: str, granted: bool) -> dict:
	if consent_type not in TYPES:
		frappe.throw(_("Unknown consent type."))
	identity = _identity(participant)
	if bool(granted) == current(session.name, identity, consent_type):
		return status(session, participant)  # no change
	now = now_datetime()
	frappe.get_doc(
		{
			"doctype": CONSENT,
			"video_session": session.name,
			"interview": session.interview,
			"consent_type": consent_type,
			"status": "Granted" if granted else "Withdrawn",
			"participant_type": "HR" if participant.kind == "HR" else participant.kind,
			"participant_user": None if identity.startswith("access:") else identity,
			"participant_name": identity,
			"policy_version": get_settings().consent_policy_version or "v1",
			"granted_on": now if granted else None,
			"withdrawn_on": None if granted else now,
			"source": "Interview Room",
			"client_fingerprint": client_fingerprint(),
		}
	).insert(ignore_permissions=True)
	audit.log(f"consent.{'granted' if granted else 'withdrawn'}", "Grid Interview Video Session", session.name,
			  consent_type=consent_type, participant=participant.kind)
	if not granted and consent_type == RECORDING:
		from grid_interview.services import recording

		recording.on_consent_withdrawn(session.name)
	return status(session, participant)


def present_participants(session) -> dict:
	"""identity -> (display name, kind) of everyone currently in the meeting (live heartbeat)."""
	from grid_interview.services import meeting

	out = {}
	for e in session.participant_events:
		if e.event_type in ("Joined", "Reconnected"):
			identity = e.user or e.details
			if meeting.is_present(session.name, identity):
				out[identity] = (frappe.utils.get_fullname(e.user) if e.user else _("Candidate"), e.participant_type)
	return out


def recording_blockers(session) -> list[str]:
	"""Names of participants currently in the meeting without a current Recording consent
	([] = may record). The candidate must be present."""
	people = present_participants(session)
	if not any(kind == "Candidate" for _name, kind in people.values()):
		return [_("Candidate (not in the meeting)")]
	return [name for identity, (name, _kind) in people.items() if not current(session.name, identity, RECORDING)]


def candidate_allows_external_processing(session) -> bool:
	"""External transcription / AI needs the candidate's own External Processing consent."""
	for e in session.participant_events:
		if e.participant_type == "Candidate" and e.event_type in ("Joined", "Reconnected"):
			if current(session.name, e.user or e.details, EXTERNAL):
				return True
	return False


def status(session, participant) -> dict:
	identity = _identity(participant)
	return {
		"recording": current(session.name, identity, RECORDING),
		"external_processing": current(session.name, identity, EXTERNAL),
		"policy_version": get_settings().consent_policy_version or "v1",
	}
