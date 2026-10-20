"""Candidate portal data. Every function takes the already-verified portal profile from
candidates.require_candidate() and returns only explicitly selected, candidate-safe fields.

Disclosure policy (what a candidate can see):
  * their own interviews (job title, round, time, status, instructions, recording notice);
    interviewer names only if Grid Interview Settings allows it;
  * an application status that never reveals unpublished decisions: HRMS applicant statuses
    such as Rejected / Accepted / Hold are shown as "Under Review" until HR publishes a
    Grid Interview Result;
  * published Grid Interview Results only (summary, next steps, requested documents);
  * never: interviewer notes, Interview Feedback, AI analysis, transcripts, internal notes,
    other candidates' data.
"""

import re
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from grid_interview.integrations import hrms_adapter
from grid_interview.utils import audit
from grid_interview.utils.settings import get_settings

SESSION = "Grid Interview Video Session"
RESULT = "Grid Interview Result"
PROFILE = "Grid Interview Portal Profile"
LOG = "Grid Interview Notification Log"

SESSION_LABELS = {
	"Scheduled": "Scheduled",
	"In Progress": "In Progress",
	"Completed": "Completed",
	"Cancelled": "Cancelled",
	"Expired": "Not attended",
	"No Show": "Not attended",
}

NOTIFICATION_LABELS = {
	"interview_scheduled": "Interview scheduled",
	"interview_rescheduled": "Interview rescheduled",
	"interview_cancelled": "Interview cancelled",
	"interview_reminder": "Interview reminder",
	"portal_result_published": "Application update",
	"portal_reference_clarification": "Reference clarification requested",
}

EDITABLE_PROFILE_FIELDS = {
	"full_name": 140,
	"phone": 30,
	"headline": 140,
	"current_location": 140,
	"linkedin_url": 200,
	"highest_qualification": 140,
	"total_experience_years": None,
}


# --------------------------------------------------------------------------- ownership
def own_applicants(profile) -> list[str]:
	return hrms_adapter.get_applicants_for_email(profile.email)


def own_session(profile, session_name: str):
	"""The session if it belongs to one of the candidate's applicants, else PermissionError."""
	row = frappe.db.get_value(SESSION, session_name, ["name", "job_applicant"], as_dict=True)
	if not row or row.job_applicant not in own_applicants(profile):
		raise frappe.PermissionError(_("Interview not found."))
	return frappe.get_doc(SESSION, session_name)


# --------------------------------------------------------------------------- dashboard
def get_dashboard(profile) -> dict:
	settings = get_settings()
	applicants = own_applicants(profile)
	now = now_datetime()
	interviews = []
	if applicants:
		for s in frappe.get_all(
			SESSION,
			filters={"job_applicant": ["in", applicants]},
			fields=["name", "interview", "job_applicant", "status", "scheduled_start", "scheduled_end",
					"instructions", "recording_eligible"],
			order_by="scheduled_start desc",
			limit=200,
		):
			interviews.append(_interview_card(s, settings, now))
	upcoming = sorted((i for i in interviews if i["upcoming"]), key=lambda i: i["start"])
	past = [i for i in interviews if not i["upcoming"]]
	return {
		"profile": {"full_name": profile.full_name, "email": profile.email},
		"upcoming": upcoming,
		"past": past,
		"applications": [_application(a, interviews) for a in applicants],
		"results": published_results(applicants),
		"hrms_available": hrms_adapter.is_available(),
	}


def _interview_card(s, settings, now) -> dict:
	try:
		info = hrms_adapter.get_interview(s.interview)
		job_title, round_name, interviewers = info.job_title or info.designation, info.interview_type, info.interviewers
	except frappe.DoesNotExistError:
		job_title = round_name = None
		interviewers = ()
	start, end = get_datetime(s.scheduled_start), get_datetime(s.scheduled_end)
	opens = start - timedelta(minutes=cint(settings.join_window_before_minutes))
	closes = end + timedelta(minutes=cint(settings.join_window_after_end_minutes))
	card = {
		"name": s.name,
		"job_applicant": s.job_applicant,
		"job_title": job_title,
		"round": round_name,
		"start": start,
		"end": end,
		"status": s.status,
		"status_label": SESSION_LABELS.get(s.status, s.status),
		"upcoming": s.status in ("Scheduled", "In Progress") and closes > now,
		"join_opens_at": opens,
		# display hint only; the room re-checks the window on the server when joining
		"can_join": s.status in ("Scheduled", "In Progress") and opens <= now <= closes,
		"instructions": s.instructions,
		"may_be_recorded": bool(cint(s.recording_eligible) and cint(settings.enable_recording)),
	}
	if cint(settings.disclose_interviewer_names):
		card["interviewers"] = [frappe.utils.get_fullname(u) for u in interviewers]
	return card


def candidate_status(applicant: str, interviews: list[dict]) -> str:
	"""Map HRMS / session state to the HR-approved candidate-facing status."""
	published = frappe.get_all(
		RESULT, filters={"job_applicant": applicant, "published": 1},
		fields=["candidate_status"], order_by="published_on desc", limit=1,
	)
	if published:
		return published[0].candidate_status
	mine = [i for i in interviews if i["job_applicant"] == applicant]
	if any(i["status"] == "In Progress" for i in mine):
		return "Interview In Progress"
	if any(i["upcoming"] for i in mine):
		return "Interview Scheduled"
	if any(i["status"] == "Completed" for i in mine):
		return "Interview Completed"
	status = frappe.db.get_value("Job Applicant", applicant, "status")
	return "Applied" if status == "Open" else "Under Review"


def _application(applicant: str, interviews: list[dict]) -> dict:
	row = hrms_adapter.get_applicant(applicant) or frappe._dict()
	job_title = frappe.db.get_value("Job Opening", row.job_title, "job_title") if row.job_title else None
	return {
		"job_applicant": applicant,
		"job_title": job_title or row.designation or _("General application"),
		"status": candidate_status(applicant, interviews),
	}


def published_results(applicants: list[str]) -> list[dict]:
	if not applicants:
		return []
	return frappe.get_all(
		RESULT,
		filters={"job_applicant": ["in", applicants], "published": 1},
		# explicit field list: internal_note (permlevel 1) is never selected
		fields=["name", "job_applicant", "candidate_status", "result_summary", "next_steps",
				"documents_requested", "published_on"],
		order_by="published_on desc",
	)


def notification_history(profile, limit=50) -> list[dict]:
	rows = frappe.get_all(
		LOG,
		filters={"recipient": profile.email, "audience": "Candidate", "status": ["in", ["Queued", "Sent"]]},
		fields=["notification_type", "status", "creation"],
		order_by="creation desc",
		limit=limit,
	)
	for r in rows:
		r["label"] = NOTIFICATION_LABELS.get(r.notification_type, r.notification_type)
	return rows


# --------------------------------------------------------------------------- profile
def get_profile(profile) -> dict:
	doc = frappe.get_doc(PROFILE, profile.name)
	data = {f: doc.get(f) for f in EDITABLE_PROFILE_FIELDS}
	data.update({"email": doc.email, "resume": bool(doc.resume)})
	return data


def update_profile(profile, values: dict) -> dict:
	doc = frappe.get_doc(PROFILE, profile.name)
	for field, max_len in EDITABLE_PROFILE_FIELDS.items():
		if field not in values:
			continue  # unknown / protected keys (email, status, user, ...) are ignored
		value = values.get(field)
		if field == "total_experience_years":
			value = flt(value)
			if not 0 <= value <= 60:
				frappe.throw(_("Experience must be between 0 and 60 years."))
		else:
			value = (value or "").strip()[:max_len]
			if re.search(r"[<>]", value):
				frappe.throw(_("{0} contains characters that are not allowed.").format(doc.meta.get_label(field)))
		if field == "linkedin_url" and value and not re.match(r"^https://([a-z]{2,3}\.)?linkedin\.com/", value):
			frappe.throw(_("Enter a LinkedIn profile URL starting with https://linkedin.com/ or https://www.linkedin.com/"))
		if field == "full_name" and len(value) < 2:
			frappe.throw(_("Enter your full name."))
		doc.set(field, value)
	doc.save(ignore_permissions=True)
	audit.log("candidate.profile_updated", PROFILE, doc.name, fields=sorted(set(values) & set(EDITABLE_PROFILE_FIELDS)))
	return get_profile(profile)


# --------------------------------------------------------------------------- documents
# Leading bytes that each allowed extension must start with (content sniffing, not trusting the name).
MAGIC = {
	"pdf": (b"%PDF-",),
	"png": (b"\x89PNG\r\n\x1a\n",),
	"jpg": (b"\xff\xd8\xff",),
	"jpeg": (b"\xff\xd8\xff",),
	"docx": (b"PK\x03\x04",),
	"doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}
CATEGORIES = ("Resume", "Document")


def safe_filename(name: str) -> str:
	base = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip("._")
	return (base or "document")[-100:]


def validate_upload(filename: str, content: bytes) -> str:
	settings = get_settings()
	ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
	allowed = {e for e in (settings.allowed_upload_extensions or "").split(",") if e}
	if ext not in allowed or ext not in MAGIC:
		frappe.throw(_("File type .{0} is not allowed. Allowed: {1}").format(ext or "?", ", ".join(sorted(allowed))))
	max_bytes = cint(settings.max_upload_size_mb or 10) * 1024 * 1024
	if not content:
		frappe.throw(_("The file is empty."))
	if len(content) > max_bytes:
		frappe.throw(_("The file is larger than {0} MB.").format(settings.max_upload_size_mb))
	if not content.startswith(MAGIC[ext]):
		frappe.throw(_("The file content does not match its .{0} extension.").format(ext))
	if settings.malware_scan_method:
		try:
			clean = frappe.get_attr(settings.malware_scan_method)(content, filename)
		except Exception:
			frappe.log_error(title="grid_interview malware scan failed")
			clean = False  # fail closed
		if not clean:
			frappe.throw(_("The file could not be accepted. Please upload a different file."))
	return ext


def list_documents(profile) -> list[dict]:
	resume_url = frappe.db.get_value(PROFILE, profile.name, "resume")
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": PROFILE, "attached_to_name": profile.name, "is_folder": 0},
		fields=["name", "file_name", "file_size", "creation", "file_url"],
		order_by="creation desc",
	)
	return [
		{"name": r.name, "file_name": r.file_name, "file_size": r.file_size, "creation": r.creation,
		 "category": "Resume" if r.file_url == resume_url else "Document"}
		for r in rows
	]


def save_document(profile, filename: str, content: bytes, category: str) -> dict:
	if category not in CATEGORIES:
		frappe.throw(_("Invalid document category."))
	settings = get_settings()
	if len(list_documents(profile)) >= cint(settings.max_documents_per_candidate or 20):
		frappe.throw(_("You have reached the maximum number of documents. Remove one first."))
	safe = safe_filename(filename)
	validate_upload(safe, content)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{category.lower()}-{safe}",
			"attached_to_doctype": PROFILE,
			"attached_to_name": profile.name,
			"attached_to_field": "resume" if category == "Resume" else None,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.flags.ignore_permissions = True
	try:
		file_doc.insert()  # Frappe also inspects content here (e.g. rejects PDFs containing JavaScript)
	except frappe.ValidationError:
		raise
	except Exception:
		frappe.log_error(title="grid_interview document could not be processed")
		frappe.throw(_("The file could not be processed. Please check that it is not damaged and try again."))
	if category == "Resume":
		frappe.db.set_value(PROFILE, profile.name, "resume", file_doc.file_url)
	audit.log("candidate.document_uploaded", "File", file_doc.name, category=category, size=len(content))
	return {"name": file_doc.name, "file_name": file_doc.file_name}


def _own_file(profile, file_name: str):
	row = frappe.db.get_value("File", file_name, ["name", "attached_to_doctype", "attached_to_name"], as_dict=True)
	if not row or row.attached_to_doctype != PROFILE or row.attached_to_name != profile.name:
		audit.log("candidate.document_access_denied", "File", file_name, result="Denied")
		raise frappe.PermissionError(_("Document not found."))
	return frappe.get_doc("File", file_name)


def get_document_content(profile, file_name: str) -> tuple[str, bytes]:
	doc = _own_file(profile, file_name)
	try:
		# encodings=[]: return raw bytes (the default tries to decode files as text, corrupting PDFs/images)
		content = doc.get_content(encodings=[])
	except (FileNotFoundError, OSError):
		frappe.log_error(title=f"grid_interview document missing on disk: {doc.name}")
		raise frappe.DoesNotExistError(_("This document is no longer available. Please upload it again."))
	audit.log("candidate.document_downloaded", "File", doc.name)
	return doc.file_name, content


def delete_document(profile, file_name: str) -> None:
	doc = _own_file(profile, file_name)
	if frappe.db.get_value(PROFILE, profile.name, "resume") == doc.file_url:
		frappe.db.set_value(PROFILE, profile.name, "resume", None)
	doc.flags.ignore_permissions = True
	doc.delete()
	audit.log("candidate.document_deleted", "File", file_name)
