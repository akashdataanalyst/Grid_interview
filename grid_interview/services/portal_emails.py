"""Subjects and contexts for non-session emails (results, references).
Emails contain no result text, transcripts or reports: only a prompt to sign in, or for a
referee the minimum needed to answer."""

import frappe
from frappe import _
from frappe.utils import escape_html

from grid_interview.services.notifications import portal_url

e = lambda v: escape_html(str(v)) if v not in (None, "") else ""  # noqa: E731


def _job_title(job_applicant: str | None) -> str:
	if not job_applicant or not frappe.db.exists("DocType", "Job Applicant"):
		return ""
	row = frappe.db.get_value("Job Applicant", job_applicant, ["job_title", "designation"], as_dict=True) or {}
	title = frappe.db.get_value("Job Opening", row.get("job_title"), "job_title") if row.get("job_title") else None
	return title or row.get("designation") or ""


def build(log) -> tuple[str, dict]:
	builder = {
		"portal_result_published": _result_published,
		"portal_reference_verification": _reference_verification,
		"portal_reference_clarification": _reference_clarification,
		"analysis_ready": _analysis_ready,
	}.get(log.notification_type)
	if not builder:
		raise ValueError(f"No email builder for {log.notification_type}")
	return builder(log)


def _result_published(log):
	doc = frappe.get_doc("Grid Interview Result", log.reference_name)
	name = frappe.db.get_value("Job Applicant", doc.job_applicant, "applicant_name")
	job = _job_title(doc.job_applicant)
	ctx = {
		"audience": "Candidate",
		"candidate_name": e(name),
		"job_title": e(job),
		"action_needed": bool((doc.documents_requested or "").strip()),
		"link": portal_url(),
	}
	return _("Update on your application{0}").format(f": {job}" if job else ""), ctx


def _reference_verification(log):
	from grid_interview.services import references

	doc = frappe.get_doc("Grid Interview Reference", log.reference_name)
	view = references.referee_view(doc)
	raw = references.issue_token(doc.name)  # fresh single-use token; never stored in the log
	ctx = {
		"audience": "Reference",
		"reference_person": e(view["reference_person"]),
		"candidate_name": e(view["candidate_name"]),
		"job_title": e(view["job_title"]),
		"relationship": e(view["relationship"]),
		"link": portal_url(f"/interview-reference?t={raw}"),
		"validity_days": frappe.db.get_single_value("Grid Interview Settings", "reference_link_validity_days") or 7,
	}
	return _("Reference request for {0}").format(view["candidate_name"]), ctx


def _analysis_ready(log):
	"""To interviewers / reviewers: no transcript or report content, only a link to review it."""
	from frappe.utils import get_url_to_form

	from grid_interview.integrations import hrms_adapter

	a = frappe.get_doc("Grid Interview Analysis", log.reference_name)
	info = hrms_adapter.get_interview(a.interview)
	ctx = {
		"audience": "Interviewer",
		"candidate_name": e(info.applicant_name),
		"job_title": e(info.job_title or info.designation),
		"interview_type": e(info.interview_type),
		"link": get_url_to_form("Grid Interview Analysis", a.name),
	}
	return _("AI draft report ready for review: {0}").format(info.applicant_name), ctx


def _reference_clarification(log):
	doc = frappe.get_doc("Grid Interview Reference", log.reference_name)
	ctx = {
		"audience": "Candidate",
		"candidate_name": e(frappe.utils.get_fullname(doc.candidate_user)),
		"reference_person": e(doc.reference_person),
		"link": portal_url("/interview-portal/references"),
	}
	return _("Please update a reference you submitted"), ctx
