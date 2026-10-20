"""HR scheduling endpoints. Authorization happens in grid_interview.services.scheduling
(role check) and in HRMS itself (Interview permissions), never in the browser."""

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, nowdate

from grid_interview.integrations import hrms_adapter
from grid_interview.services import scheduling

SESSION = "Grid Interview Video Session"
LOG = "Grid Interview Notification Log"


@frappe.whitelist(methods=["POST"])
def schedule_interview(
	job_applicant: str,
	interview_type: str,
	scheduled_on: str,
	from_time: str,
	to_time: str,
	interviewers: str | list,
	instructions: str | None = None,
	recording_eligible: int = 0,
	allow_overlap: int = 0,
):
	return scheduling.schedule_interview(
		job_applicant, interview_type, scheduled_on, from_time, to_time, interviewers,
		instructions=instructions, recording_eligible=recording_eligible, allow_overlap=allow_overlap,
	)


@frappe.whitelist(methods=["POST"])
def reschedule_interview(video_session: str, scheduled_on: str, from_time: str, to_time: str,
						 reason: str | None = None, candidate_message: str | None = None, allow_overlap: int = 0):
	return scheduling.reschedule(video_session, scheduled_on, from_time, to_time, reason=reason,
								 candidate_message=candidate_message, allow_overlap=allow_overlap)


@frappe.whitelist(methods=["POST"])
def cancel_interview(video_session: str, reason: str | None = None, candidate_message: str | None = None):
	return scheduling.cancel(video_session, reason=reason, candidate_message=candidate_message)


@frappe.whitelist(methods=["POST"])
def attach_interview(interview: str, instructions: str | None = None, recording_eligible: int = 0):
	return scheduling.attach_existing(interview, instructions=instructions, recording_eligible=recording_eligible)


@frappe.whitelist(methods=["POST"])
def resend_invitation(video_session: str):
	return {"recipients": len(scheduling.resend_invitation(video_session))}


@frappe.whitelist(methods=["GET"])
def get_overview(from_date: str | None = None, to_date: str | None = None, status: str | None = None):
	"""Sessions in a date range with interview details and notification counts for the HR page."""
	scheduling.ensure_can_schedule()
	from_date = getdate(from_date or add_days(nowdate(), -7))
	to_date = getdate(to_date or add_days(nowdate(), 30))
	if (to_date - from_date).days > 366:
		frappe.throw(_("Choose a date range of at most one year."))
	filters = {"scheduled_start": ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]}
	if status:
		filters["status"] = status
	sessions = frappe.get_all(
		SESSION,
		filters=filters,
		fields=["name", "interview", "job_applicant", "status", "scheduled_start", "scheduled_end",
				"candidate_joined", "interviewer_joined", "recording_eligible"],
		order_by="scheduled_start asc",
		limit=500,
	)
	if not sessions:
		return []
	counts = {}
	for row in frappe.get_all(
		LOG,
		filters={"reference_doctype": SESSION, "reference_name": ["in", [s.name for s in sessions]]},
		fields=["reference_name", "status", {"COUNT": "name", "as": "n"}],
		group_by="reference_name, status",
	):
		counts.setdefault(row.reference_name, {})[row.status] = row.n
	out = []
	for s in sessions:
		try:
			info = hrms_adapter.get_interview(s.interview)
		except frappe.DoesNotExistError:
			continue
		out.append(
			{
				**s,
				"applicant_name": info.applicant_name,
				"job_title": info.job_title or info.designation,
				"interview_type": info.interview_type,
				"interviewers": info.interviewers,
				"interview_status": info.status,
				"notifications": counts.get(s.name, {}),
			}
		)
	return out


@frappe.whitelist(methods=["GET"])
def get_notifications(video_session: str):
	scheduling.ensure_can_schedule()
	return frappe.get_all(
		LOG,
		filters={"reference_doctype": SESSION, "reference_name": video_session},
		fields=["notification_type", "audience", "recipient", "status", "attempts", "last_error", "creation", "modified"],
		order_by="creation desc",
		limit=200,
	)


@frappe.whitelist(methods=["GET"])
def search_interviewers(txt: str = "", limit: int = 20):
	scheduling.ensure_can_schedule()
	users = frappe.get_all(
		"Has Role",
		filters={"role": "Interviewer", "parenttype": "User", "parent": ["like", f"%{txt}%"]},
		pluck="parent",
		limit=cint(limit) or 20,
	)
	return frappe.get_all(
		"User",
		filters={"name": ["in", users or [""]], "enabled": 1, "user_type": "System User"},
		fields=["name as value", "full_name as description"],
	)


@frappe.whitelist(methods=["GET"])
def get_unmanaged_interviews():
	"""Pending future HRMS Interviews created in Desk that Grid Interview does not manage yet."""
	scheduling.ensure_can_schedule()
	managed = frappe.get_all(SESSION, pluck="interview")
	return frappe.get_list(  # permission-filtered for the current user
		"Interview",
		filters={"docstatus": 0, "status": "Pending", "scheduled_on": [">=", nowdate()],
				 "name": ["not in", managed or [""]]},
		fields=["name", "job_applicant", "interview_type", "scheduled_on", "from_time", "to_time"],
		order_by="scheduled_on asc",
		limit=100,
	)
