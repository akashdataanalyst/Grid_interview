"""Candidate references and verification.

Candidate: add / edit / delete own references while they are not locked.
HR: send a verification request (locks the reference), request clarification (unlocks it),
record notes. The referee gets a single-use, expiring link and sees only the candidate's name,
the position and the relationship the candidate stated: never the application, interview,
transcript or AI data.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, now_datetime, validate_email_address

from grid_interview.services import notifications
from grid_interview.utils import audit
from grid_interview.utils.security import hash_token, new_token
from grid_interview.utils.settings import get_settings

REFERENCE = "Grid Interview Reference"
EDITABLE = {"reference_person": 140, "designation": 140, "company": 140, "relationship": 140,
			"email": 140, "phone": 30, "candidate_notes": 1000}
CANDIDATE_EDITABLE_STATUSES = ("Submitted", "Clarification Requested")
HR_ROLES = {"Grid Interview Administrator", "Grid Interview HR User", "System Manager"}


def _clean(values: dict) -> dict:
	out = {}
	for field, max_len in EDITABLE.items():
		if field in values:
			value = (values.get(field) or "").strip()[:max_len]
			if "<" in value or ">" in value:
				frappe.throw(_("Please remove < and > characters."))
			out[field] = value
	if "email" in out:
		out["email"] = out["email"].lower()
		if not validate_email_address(out["email"]):
			frappe.throw(_("Enter the referee's valid official email."))
	return out


# --------------------------------------------------------------------------- candidate side
def list_references(profile) -> list[dict]:
	return frappe.get_all(
		REFERENCE,
		filters={"candidate_user": profile.user},
		# explicit fields: internal notes and the referee's response are never returned
		fields=["name", "reference_person", "designation", "company", "relationship", "email", "phone",
				"candidate_notes", "verification_status", "locked", "clarification_request", "submitted_on"],
		order_by="creation asc",
	)


def add_reference(profile, values: dict, job_applicant: str | None = None) -> str:
	from grid_interview.services.portal import own_applicants

	if not cint(values.get("consent_confirmed")):
		frappe.throw(_("Please confirm that the referee agreed to be contacted."))
	data = _clean(values)
	for required in ("reference_person", "email", "relationship"):
		if not data.get(required):
			frappe.throw(_("{0} is required.").format(frappe.get_meta(REFERENCE).get_label(required)))
	if frappe.db.count(REFERENCE, {"candidate_user": profile.user}) >= cint(get_settings().max_references or 5):
		frappe.throw(_("You have reached the maximum number of references."))
	if data["email"] == (profile.email or "").lower():
		frappe.throw(_("You cannot be your own referee."))
	if job_applicant and job_applicant not in own_applicants(profile):
		raise frappe.PermissionError(_("Application not found."))
	doc = frappe.get_doc(
		{
			"doctype": REFERENCE,
			"candidate_user": profile.user,
			"job_applicant": job_applicant,
			"consent_confirmed": 1,
			"submitted_on": now_datetime(),
			"verification_status": "Submitted",
			**data,
		}
	).insert(ignore_permissions=True)
	audit.log("reference.submitted", REFERENCE, doc.name)
	return doc.name


def _own_editable(profile, name: str):
	doc = frappe.get_doc(REFERENCE, name) if frappe.db.exists(REFERENCE, name) else None
	if not doc or doc.candidate_user != profile.user:
		raise frappe.PermissionError(_("Reference not found."))
	if doc.locked or doc.verification_status not in CANDIDATE_EDITABLE_STATUSES:
		frappe.throw(_("This reference is being verified and can no longer be changed. Contact HR for corrections."))
	return doc


def update_reference(profile, name: str, values: dict) -> None:
	doc = _own_editable(profile, name)
	doc.update(_clean(values))
	if doc.verification_status == "Clarification Requested":
		doc.verification_status = "Submitted"
	doc.save(ignore_permissions=True)
	audit.log("reference.updated", REFERENCE, name)


def delete_reference(profile, name: str) -> None:
	doc = _own_editable(profile, name)
	doc.flags.ignore_permissions = True
	doc.delete()
	audit.log("reference.deleted", REFERENCE, name)


# --------------------------------------------------------------------------- HR side
def ensure_hr():
	if frappe.session.user != "Administrator" and not HR_ROLES.intersection(frappe.get_roles()):
		raise frappe.PermissionError(_("Only HR can manage reference verification."))


def send_verification(name: str) -> None:
	ensure_hr()
	doc = frappe.get_doc(REFERENCE, name)
	if doc.verification_status in ("Verified", "Declined"):
		frappe.throw(_("This reference is already {0}.").format(doc.verification_status.lower()))
	doc.locked = 1
	doc.verification_status = "Verification Sent"
	doc.verification_sent_on = now_datetime()
	doc.save(ignore_permissions=True)
	# The token is created at render time (see issue_token) so that it is never stored in
	# the notification log; a retry re-issues a fresh token.
	notifications.notify(
		"portal_reference_verification", doc.name, doc.email, "Reference",
		f"reference_verification:{doc.name}:{doc.verification_sent_on}",
		reference_doctype=REFERENCE,
	)
	audit.log("reference.verification_sent", REFERENCE, name)


def issue_token(name: str) -> str:
	raw, hashed = new_token()
	days = cint(get_settings().reference_link_validity_days or 7)
	frappe.db.set_value(
		REFERENCE, name, {"verification_token_hash": hashed, "verification_expires_on": add_days(now_datetime(), days)}
	)
	return raw


def request_clarification(name: str, message: str) -> None:
	ensure_hr()
	message = (message or "").strip()
	if not message:
		frappe.throw(_("Describe what the candidate should clarify."))
	doc = frappe.get_doc(REFERENCE, name)
	doc.locked = 0
	doc.verification_status = "Clarification Requested"
	doc.clarification_request = message[:1000]
	doc.verification_token_hash = None
	doc.save(ignore_permissions=True)
	candidate_email = frappe.db.get_value("User", doc.candidate_user, "email")
	notifications.notify(
		"portal_reference_clarification", doc.name, candidate_email, "Candidate",
		f"reference_clarification:{doc.name}:{now_datetime().isoformat()}",
		reference_doctype=REFERENCE,
	)
	audit.log("reference.clarification_requested", REFERENCE, name)


# --------------------------------------------------------------------------- referee side (guest, token)
def resolve_token(raw: str, audit_failure: bool = True):
	"""The reference for a valid, unexpired, unused token, else InvalidAccessToken (one message for all).
	The guest GET page passes audit_failure=False so anonymous page loads cannot fill the audit log;
	the rate-limited POST still audits failures."""
	from grid_interview.exceptions import InvalidAccessToken

	name = frappe.db.get_value(REFERENCE, {"verification_token_hash": hash_token(raw or "")}, "name") if raw else None
	doc = frappe.get_doc(REFERENCE, name) if name else None
	if (
		not doc
		or doc.verification_status != "Verification Sent"
		or not doc.verification_expires_on
		or get_datetime(doc.verification_expires_on) < now_datetime()
	):
		if audit_failure:
			audit.log("reference.token_rejected", actor="Guest", result="Denied")
		raise InvalidAccessToken(_("This link is invalid or has expired."))
	return doc


def referee_view(doc) -> dict:
	"""The only data a referee is shown."""
	from grid_interview.integrations import hrms_adapter

	candidate = frappe.utils.get_fullname(doc.candidate_user)
	job_title = None
	if doc.job_applicant and hrms_adapter.is_available():
		applicant = hrms_adapter.get_applicant(doc.job_applicant) or {}
		job_title = frappe.db.get_value("Job Opening", applicant.get("job_title"), "job_title") if applicant.get("job_title") else applicant.get("designation")
	return {
		"candidate_name": candidate,
		"job_title": job_title,
		"relationship": doc.relationship,
		"reference_person": doc.reference_person,
		"company": doc.company,
	}


RESPONSES = {"Yes": "Verified", "Partly": "Verified", "No": "Unable to Verify"}


def submit_response(raw: str, relationship_confirmed: str, period: str = "", comments: str = "",
					would_recommend: str = "", decline: int = 0) -> None:
	doc = resolve_token(raw)
	if cint(decline):
		status = "Declined"
		text = _("The referee declined to provide a reference.")
	else:
		if relationship_confirmed not in RESPONSES:
			frappe.throw(_("Please answer whether you know the candidate in the stated capacity."))
		status = RESPONSES[relationship_confirmed]
		clean = lambda v, n: (v or "").replace("<", "").replace(">", "").strip()[:n]  # noqa: E731
		text = "\n".join(
			[
				f"Relationship confirmed: {relationship_confirmed}",
				f"Period worked together: {clean(period, 200)}",
				f"Would recommend: {clean(would_recommend, 40)}",
				f"Comments: {clean(comments, 4000)}",
			]
		)
	doc.verification_status = status
	doc.reference_response = text
	doc.verified_on = now_datetime()
	doc.verification_token_hash = None  # single use
	doc.flags.ignore_permissions = True
	doc.save()
	audit.log("reference.response_received", REFERENCE, doc.name, actor="Guest", status=status)
