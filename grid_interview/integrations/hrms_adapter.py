"""Read-only adapter over the standard HRMS recruitment DocTypes (verified on hrms 16.50.0).

grid_interview never adds fields to these DocTypes. Everything else in the app talks to HRMS
only through this module, so a missing HRMS (or a future schema change) is handled in one place.

Verified schema used here:
  Interview: interview_type, job_applicant, job_opening, designation, resume_link, status,
             scheduled_on (Date), from_time / to_time (Time, system time zone), docstatus,
             interview_details -> Interview Detail.interviewer (User)
  Interview Type (the v16 name of "Interview Round"): expected_skill_set -> Expected Skill Set.skill
  Job Applicant: applicant_name, email_id, phone_number, job_title (-> Job Opening), designation,
                 status, resume_attachment, resume_link
  Job Opening: job_title, designation, department
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import frappe
from frappe.query_builder import Order
from frappe.utils import get_datetime, getdate
from pypika.functions import Lower

from grid_interview.exceptions import HRMSNotAvailable

INTERVIEW = "Interview"
JOB_APPLICANT = "Job Applicant"


@dataclass(frozen=True)
class InterviewInfo:
	name: str
	status: str
	docstatus: int
	interview_type: str
	job_applicant: str
	applicant_name: str
	applicant_email: str
	job_opening: str | None
	job_title: str | None
	designation: str | None
	start: datetime
	end: datetime
	interviewers: tuple[str, ...]

	@property
	def is_cancelled(self) -> bool:
		return self.status == "Cancelled" or self.docstatus == 2


def is_available() -> bool:
	return "hrms" in frappe.get_installed_apps() and bool(frappe.db.exists("DocType", INTERVIEW))


def ensure_available():
	if not is_available():
		raise HRMSNotAvailable(frappe._("HRMS is not installed on this site; interview scheduling is unavailable."))


def interview_window(scheduled_on, from_time, to_time) -> tuple[datetime, datetime]:
	"""Naive datetimes in the system time zone (Frappe's storage convention).
	An end time earlier than the start time means the interview runs past midnight."""
	day = getdate(scheduled_on)
	start = get_datetime(f"{day} {_time_str(from_time)}")
	end = get_datetime(f"{day} {_time_str(to_time)}")
	if end <= start:
		end += timedelta(days=1)
	return start, end


def _time_str(value) -> str:
	if isinstance(value, timedelta):
		total = int(value.total_seconds())
		return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
	return str(value)


def get_interview(name: str) -> InterviewInfo:
	ensure_available()
	row = frappe.db.get_value(
		INTERVIEW,
		name,
		["name", "status", "docstatus", "interview_type", "job_applicant", "job_opening", "designation",
		 "scheduled_on", "from_time", "to_time"],
		as_dict=True,
	)
	if not row:
		raise frappe.DoesNotExistError(frappe._("Interview {0} not found").format(name))
	applicant = frappe.db.get_value(
		JOB_APPLICANT, row.job_applicant, ["applicant_name", "email_id", "job_title", "designation"], as_dict=True
	) or frappe._dict()
	job_opening = row.job_opening or applicant.job_title
	job_title = frappe.db.get_value("Job Opening", job_opening, "job_title") if job_opening else None
	start, end = interview_window(row.scheduled_on, row.from_time, row.to_time)
	return InterviewInfo(
		name=row.name,
		status=row.status,
		docstatus=row.docstatus,
		interview_type=row.interview_type,
		job_applicant=row.job_applicant,
		applicant_name=applicant.applicant_name or "",
		applicant_email=(applicant.email_id or "").strip().lower(),
		job_opening=job_opening,
		job_title=job_title,
		designation=row.designation or applicant.designation,
		start=start,
		end=end,
		interviewers=tuple(get_interviewers(row.name)),
	)


def get_interviewers(interview: str) -> list[str]:
	ensure_available()
	return frappe.get_all(
		"Interview Detail",
		filters={"parent": interview, "parenttype": INTERVIEW},
		pluck="interviewer",
		order_by="idx asc",
	)


def is_interviewer(user: str, interview: str) -> bool:
	if not is_available() or not user or user == "Guest":
		return False
	return bool(
		frappe.db.exists("Interview Detail", {"parent": interview, "parenttype": INTERVIEW, "interviewer": user})
	)


def get_applicants_for_email(email: str) -> list[str]:
	"""Job Applicants whose email matches exactly, ignoring case. The email is proven by account login.
	Not a LIKE filter: "_" and "%" are wildcards there, so "a.b@x.com" would also match "a_b@x.com"."""
	if not is_available() or not (email or "").strip():
		return []
	ja = frappe.qb.DocType(JOB_APPLICANT)
	return (
		frappe.qb.from_(ja)
		.select(ja.name)
		.where(Lower(ja.email_id) == email.strip().lower())
		.orderby(ja.creation, order=Order.desc)
	).run(pluck=True)


def get_expected_skills(interview_type: str) -> list[dict]:
	ensure_available()
	return frappe.get_all(
		"Expected Skill Set",
		filters={"parent": interview_type, "parenttype": "Interview Type"},
		fields=["skill", "description"],
		order_by="idx asc",
	)


def get_applicant(name: str) -> dict:
	ensure_available()
	return frappe.db.get_value(
		JOB_APPLICANT,
		name,
		["name", "applicant_name", "email_id", "phone_number", "job_title", "designation", "status"],
		as_dict=True,
	)
