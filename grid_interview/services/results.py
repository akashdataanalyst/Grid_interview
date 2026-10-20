"""Candidate-facing results. Only an authorised human (HR) publishes; nothing is published
automatically and AI output never becomes a Result. The email only says that an update is
available: the result text is shown after the candidate signs in."""

import frappe
from frappe import _
from frappe.utils import now_datetime, strip_html

from grid_interview.services import notifications
from grid_interview.utils import audit

RESULT = "Grid Interview Result"
PUBLISH_ROLES = {"Grid Interview Administrator", "Grid Interview HR User"}


def can_publish(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user == "Administrator" or bool(PUBLISH_ROLES.intersection(frappe.get_roles(user)))


def publish(name: str) -> None:
	if not can_publish():
		raise frappe.PermissionError(_("Only HR can publish results to candidates."))
	doc = frappe.get_doc(RESULT, name)
	doc.check_permission("write")
	if doc.published:
		return
	if not strip_html(doc.result_summary or "").strip() and doc.candidate_status in ("Selected", "Not Selected", "On Hold", "Next Round"):
		frappe.throw(_("Write the result summary the candidate will see before publishing."))
	doc.flags.grid_interview_publish = True
	doc.published = 1
	doc.published_on = now_datetime()
	doc.published_by = frappe.session.user
	doc.save()
	email = frappe.db.get_value("Job Applicant", doc.job_applicant, "email_id")
	if email:
		notifications.notify(
			"portal_result_published", doc.name, email.strip().lower(), "Candidate",
			f"result_published:{doc.name}:{doc.published_on.isoformat()}",
			reference_doctype=RESULT,
		)
	audit.log("result.published", RESULT, name, candidate_status=doc.candidate_status)


def unpublish(name: str) -> None:
	if not can_publish():
		raise frappe.PermissionError(_("Only HR can change published results."))
	doc = frappe.get_doc(RESULT, name)
	doc.check_permission("write")
	if not doc.published:
		return
	doc.flags.grid_interview_publish = True
	doc.published = 0
	doc.save()
	audit.log("result.unpublished", RESULT, name)
