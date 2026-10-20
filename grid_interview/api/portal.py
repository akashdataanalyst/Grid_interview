"""Candidate portal and referee endpoints.

Candidate endpoints start with candidates.require_candidate(): the caller must be signed in
with the Grid Interview Candidate role, and every record is resolved from the session user,
never from an id the browser supplies alone. Guest endpoints are rate limited and return
responses that do not reveal whether an account or token exists.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from grid_interview.services import candidates, portal, references


# --------------------------------------------------------------------------- guest
@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=5, seconds=60 * 60)
def register(full_name: str, email: str, accept_privacy_notice: int = 0):
	return {"message": candidates.register(full_name, email, accept_privacy_notice)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def submit_reference_response(token: str, relationship_confirmed: str = "", period: str = "",
							  comments: str = "", would_recommend: str = "", decline: int = 0):
	references.submit_response(token, relationship_confirmed, period, comments, would_recommend, decline)
	return {"message": _("Thank you. Your response has been recorded.")}


# --------------------------------------------------------------------------- candidate
@frappe.whitelist(methods=["GET"])
def get_dashboard():
	return portal.get_dashboard(candidates.require_candidate())


@frappe.whitelist(methods=["GET"])
def get_profile():
	profile = candidates.require_candidate()
	return {"profile": portal.get_profile(profile), "documents": portal.list_documents(profile)}


@frappe.whitelist(methods=["POST"])
def update_profile(**values):
	values.pop("cmd", None)
	return portal.update_profile(candidates.require_candidate(), values)


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=30, seconds=60 * 60)
def upload_document(category: str = "Document"):
	profile = candidates.require_candidate()
	upload = frappe.request.files.get("file") if frappe.request else None
	if not upload:
		frappe.throw(_("Choose a file to upload."))
	return portal.save_document(profile, upload.filename or "", upload.stream.read(), category)


@frappe.whitelist(methods=["GET"])
def download_document(file: str):
	name, content = portal.get_document_content(candidates.require_candidate(), file)
	frappe.local.response.filename = name
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"


@frappe.whitelist(methods=["POST"])
def delete_document(file: str):
	portal.delete_document(candidates.require_candidate(), file)
	return {"ok": True}


@frappe.whitelist(methods=["GET"])
def list_references():
	return references.list_references(candidates.require_candidate())


@frappe.whitelist(methods=["POST"])
def add_reference(job_applicant: str | None = None, **values):
	values.pop("cmd", None)
	return {"name": references.add_reference(candidates.require_candidate(), values, job_applicant or None)}


@frappe.whitelist(methods=["POST"])
def update_reference(name: str, **values):
	values.pop("cmd", None)
	references.update_reference(candidates.require_candidate(), name, values)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def delete_reference(name: str):
	references.delete_reference(candidates.require_candidate(), name)
	return {"ok": True}


@frappe.whitelist(methods=["GET"])
def get_notification_history():
	return portal.notification_history(candidates.require_candidate())


@frappe.whitelist(methods=["POST"])
def logout_other_sessions():
	from frappe.sessions import clear_sessions

	profile = candidates.require_candidate()
	clear_sessions(profile.user, keep_current=True, force=True)
	from grid_interview.utils import audit

	audit.log("candidate.sessions_cleared", "User", profile.user)
	return {"ok": True}
