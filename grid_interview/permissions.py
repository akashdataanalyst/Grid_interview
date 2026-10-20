"""Row-level rules for app DocTypes that interviewers can read.

Administrators, HR users, reviewers and System Managers see every row. A user whose only
relevant role is HRMS "Interviewer" sees rows only for interviews they are assigned to.
Candidates never read these DocTypes directly; portal APIs check access explicitly.
"""

import frappe

from grid_interview.integrations import hrms_adapter
from grid_interview.setup.install import ADMIN_ROLE, CANDIDATE_ROLE, HR_ROLE, REVIEWER_ROLE

FULL_ACCESS_ROLES = {ADMIN_ROLE, HR_ROLE, REVIEWER_ROLE, "System Manager"}


def has_full_access(user: str | None = None) -> bool:
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return bool(FULL_ACCESS_ROLES.intersection(frappe.get_roles(user)))


def can_open_app(user=None) -> bool:
	user = user or frappe.session.user
	return has_full_access(user) or "Interviewer" in frappe.get_roles(user)


def website_user_home_page(user=None):
	"""Portal home for candidate Website Users only; Desk users keep Frappe's default."""
	user = user or frappe.session.user
	if user in ("Guest", "Administrator"):
		return None
	if frappe.db.get_value("User", user, "user_type") != "Website User":
		return None
	if CANDIDATE_ROLE in frappe.get_roles(user):
		return "interview-portal"
	return None


def _assigned_interviews_condition(table: str, user: str) -> str:
	if not hrms_adapter.is_available():
		return "1=0"
	return (
		f"`tab{table}`.`interview` in (select `parent` from `tabInterview Detail` "
		f"where `parenttype` = 'Interview' and `interviewer` = {frappe.db.escape(user)})"
	)


def video_session_query(user=None, doctype=None):
	return interview_linked_query(user, doctype or "Grid Interview Video Session")


def interview_linked_query(user=None, doctype=None):
	"""Frappe passes `doctype=` (frappe.model.db_query / frappe.database.query, v16).
	Video Session, Transcript and Analysis all carry an `interview` Dynamic Link field."""
	user = user or frappe.session.user
	if has_full_access(user):
		return ""
	return _assigned_interviews_condition(doctype, user)


def has_interview_linked_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if has_full_access(user):
		return True
	interview = doc.get("interview") if not isinstance(doc, str) else None
	return bool(interview) and hrms_adapter.is_interviewer(user, interview)
