"""HR dashboard, candidate timeline and the final-decision step.

Data comes from standard HRMS records (Interview, Interview Feedback, Job Applicant, Job Opening)
joined to Grid Interview's own records. Grid Interview HR / Reviewers / Administrators see
everything; a user with only the HRMS Interviewer role sees only interviews they are assigned to.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, get_datetime, getdate, now_datetime, nowdate

from grid_interview.integrations import hrms_adapter
from grid_interview.permissions import has_full_access
from grid_interview.setup.install import ADMIN_ROLE, HR_ROLE
from grid_interview.utils import audit

SESSION = "Grid Interview Video Session"
BUCKETS = (
	"today", "upcoming", "in_progress", "completed", "feedback_pending", "awaiting_decision",
	"processing", "review_pending", "transcription_failed", "analysis_failed",
)
FILTER_KEYS = ("from_date", "to_date", "job_opening", "department", "designation", "interview_type", "interviewer", "status")


def _scope_user():
	user = frappe.session.user
	if has_full_access(user):
		return None
	if "Interviewer" in frappe.get_roles(user):
		return user
	raise frappe.PermissionError(_("You are not allowed to view interview dashboards."))


def _rows(filters: dict) -> list[dict]:
	hrms_adapter.ensure_available()
	scope = _scope_user()
	f = {k: (filters or {}).get(k) for k in FILTER_KEYS}
	from_date = getdate(f["from_date"] or add_days(nowdate(), -30))
	to_date = getdate(f["to_date"] or add_days(nowdate(), 30))
	if (to_date - from_date).days > 366:
		frappe.throw(_("Choose a date range of at most one year."))
	params = {"from": f"{from_date} 00:00:00", "to": f"{to_date} 23:59:59"}
	conditions = ["s.scheduled_start between %(from)s and %(to)s"]
	for key, column in (("job_opening", "jo.name"), ("department", "jo.department"), ("designation", "i.designation"),
						("interview_type", "i.interview_type"), ("status", "s.status")):
		if f[key]:
			conditions.append(f"{column} = %({key})s")
			params[key] = f[key]
	for user, key in ((f["interviewer"], "interviewer"), (scope, "scope_user")):
		if user:
			conditions.append(
				f"exists (select 1 from `tabInterview Detail` d where d.parent = i.name and d.parenttype = 'Interview' "
				f"and d.interviewer = %({key})s)"
			)
			params[key] = user
	# all values are bound parameters; only fixed column names are interpolated
	return frappe.db.sql(
		f"""
		select s.name, s.interview, s.job_applicant, s.status, s.scheduled_start, s.scheduled_end,
			s.processing_status, s.recording_status, s.candidate_joined, s.interviewer_joined,
			i.interview_type, i.status as interview_status, i.docstatus as interview_docstatus, i.designation,
			ja.applicant_name, jo.name as job_opening, jo.job_title, jo.department
		from `tabGrid Interview Video Session` s
		join `tabInterview` i on i.name = s.interview and s.interview_doctype = 'Interview'
		left join `tabJob Applicant` ja on ja.name = i.job_applicant
		left join `tabJob Opening` jo on jo.name = coalesce(i.job_opening, ja.job_title)
		where {" and ".join(conditions)}
		order by s.scheduled_start desc
		limit 2000
		""",
		params,
		as_dict=True,
	)


def _feedback_state(interviews: list[str]) -> dict:
	if not interviews:
		return {}
	assigned, submitted = {}, {}
	for row in frappe.get_all("Interview Detail", filters={"parent": ["in", interviews], "parenttype": "Interview"},
							  fields=["parent", "interviewer"]):
		assigned.setdefault(row.parent, set()).add(row.interviewer)
	for row in frappe.get_all("Interview Feedback", filters={"interview": ["in", interviews], "docstatus": 1},
							  fields=["interview", "interviewer"]):
		submitted.setdefault(row.interview, set()).add(row.interviewer)
	return {i: {"assigned": sorted(assigned.get(i, ())), "submitted": sorted(submitted.get(i, ()))} for i in interviews}


def _latest(doctype: str, link_field: str, values: list[str], fields: list[str]) -> dict:
	if not values:
		return {}
	out = {}
	for row in frappe.get_all(doctype, filters={link_field: ["in", values]}, fields=[link_field, *fields],
							  order_by="creation asc"):
		out[row[link_field]] = row  # later rows win
	return out


def classify(rows: list[dict]) -> dict:
	now = now_datetime()
	today = getdate()
	sessions = [r.name for r in rows]
	feedback = _feedback_state([r.interview for r in rows])
	transcripts = _latest("Grid Interview Transcript", "video_session", sessions, ["name", "status"])
	analyses = _latest("Grid Interview Analysis", "interview", [r.interview for r in rows], ["name", "status", "review_status"])
	buckets = {b: [] for b in BUCKETS}
	for r in rows:
		fb = feedback.get(r.interview, {"assigned": [], "submitted": []})
		r["feedback_submitted"] = len(fb["submitted"])
		r["feedback_expected"] = len(fb["assigned"])
		t = transcripts.get(r.name)
		a = analyses.get(r.interview)
		r["transcript_status"] = t.status if t else None
		r["analysis"] = a.name if a else None
		r["analysis_status"] = a.status if a else None
		r["review_status"] = a.review_status if a else None
		start = get_datetime(r.scheduled_start)
		if getdate(start) == today and r.status in ("Scheduled", "In Progress"):
			buckets["today"].append(r)
		if r.status == "Scheduled" and start > now:
			buckets["upcoming"].append(r)
		if r.status == "In Progress":
			buckets["in_progress"].append(r)
		if r.status == "Completed":
			buckets["completed"].append(r)
			if r["feedback_submitted"] < r["feedback_expected"]:
				buckets["feedback_pending"].append(r)
			if r.interview_docstatus == 0 and r.interview_status in ("Pending", "Under Review"):
				buckets["awaiting_decision"].append(r)
		if r.processing_status == "In Progress":
			buckets["processing"].append(r)
		if a and a.status == "Draft" and a.review_status == "Pending Review":
			buckets["review_pending"].append(r)
		if t and t.status == "Failed" or (r.processing_status == "Failed" and not a):
			buckets["transcription_failed"].append(r)
		if a and a.status == "Failed":
			buckets["analysis_failed"].append(r)
	return buckets


def summary(filters: dict) -> dict:
	rows = _rows(filters)
	buckets = classify(rows)
	return {"counts": {k: len(v) for k, v in buckets.items()}, "total": len(rows)}


def bucket_rows(bucket: str, filters: dict) -> list[dict]:
	if bucket not in BUCKETS and bucket != "all":
		frappe.throw(_("Unknown list."))
	rows = _rows(filters)
	if bucket == "all":
		classify(rows)
		return rows[:500]
	return classify(rows)[bucket][:500]


# --------------------------------------------------------------------------- candidate timeline
def timeline(job_applicant: str) -> list[dict]:
	if not has_full_access():
		raise frappe.PermissionError(_("Only HR can view candidate timelines."))
	hrms_adapter.ensure_available()
	applicant = frappe.db.get_value("Job Applicant", job_applicant, ["name", "applicant_name", "creation", "email_id"], as_dict=True)
	if not applicant:
		frappe.throw(_("Job Applicant not found."))
	events = [{"time": applicant.creation, "kind": "Application", "title": _("Application received")}]
	interviews = frappe.get_all("Interview", filters={"job_applicant": job_applicant},
								fields=["name", "interview_type", "creation", "status", "docstatus", "modified"])
	sessions = frappe.get_all(SESSION, filters={"job_applicant": job_applicant}, fields=["name", "interview"])
	session_names = [s.name for s in sessions]
	for i in interviews:
		events.append({"time": i.creation, "kind": "Interview", "title": _("{0} interview created").format(i.interview_type),
					   "link": ["Interview", i.name]})
		if i.docstatus == 1:
			events.append({"time": i.modified, "kind": "Decision", "title": _("{0} interview submitted: {1}").format(i.interview_type, i.status),
						   "link": ["Interview", i.name]})
	for fb in frappe.get_all("Interview Feedback", filters={"job_applicant": job_applicant, "docstatus": 1},
							 fields=["name", "interviewer", "result", "modified"]):
		events.append({"time": fb.modified, "kind": "Feedback",
					   "title": _("Feedback by {0}: {1}").format(frappe.utils.get_fullname(fb.interviewer), fb.result),
					   "link": ["Interview Feedback", fb.name]})
	labels = {
		"interview.scheduled": _("Interview scheduled"), "interview.rescheduled": _("Interview rescheduled"),
		"interview.cancelled": _("Interview cancelled"), "interview.rescheduled_outside": _("Rescheduled in HRMS"),
		"interview.cancelled_outside": _("Cancelled in HRMS"), "meeting.joined": _("Joined the interview"),
		"meeting.started": _("Interview started"), "meeting.ended": _("Interview ended"),
		"meeting.completed_by_timeout": _("Interview closed after the window"), "meeting.no_show": _("Candidate did not join"),
		"meeting.expired": _("Interview window passed"), "recording.started": _("Recording started"),
		"recording.stored": _("Recording stored"), "recording.consent_withdrawn": _("Recording consent withdrawn"),
		"access.link_requested": _("Join link sent"),
	}
	refs = session_names + frappe.get_all("Grid Interview Recording", filters={"video_session": ["in", session_names or [""]]}, pluck="name")
	for row in frappe.get_all("Grid Interview Audit Log", filters={"reference_name": ["in", refs or [""]], "action": ["in", list(labels)]},
							  fields=["action", "actor", "creation", "metadata"], order_by="creation asc", limit=500):
		detail = ""
		if row.action == "meeting.joined":
			import json

			detail = (json.loads(row.metadata or "{}") or {}).get("kind", "")
		events.append({"time": row.creation, "kind": "Interview room" if row.action.startswith(("meeting", "recording")) else "Scheduling",
					   "title": labels[row.action], "detail": detail})
	for t in frappe.get_all("Grid Interview Transcript", filters={"video_session": ["in", session_names or [""]], "status": "Completed"},
							fields=["name", "completed_on"]):
		events.append({"time": t.completed_on, "kind": "Processing", "title": _("Transcript ready"), "link": ["Grid Interview Transcript", t.name]})
	for a in frappe.get_all("Grid Interview Analysis", filters={"interview": ["in", [i.name for i in interviews] or [""]]},
							fields=["name", "generated_on", "review_status", "reviewed_on", "reviewed_by"]):
		if a.generated_on:
			events.append({"time": a.generated_on, "kind": "Processing", "title": _("AI draft report generated"),
						   "link": ["Grid Interview Analysis", a.name]})
		if a.reviewed_on:
			events.append({"time": a.reviewed_on, "kind": "Review", "title": _("AI draft {0} by {1}").format(
				_(a.review_status).lower(), frappe.utils.get_fullname(a.reviewed_by)), "link": ["Grid Interview Analysis", a.name]})
	for r in frappe.get_all("Grid Interview Result", filters={"job_applicant": job_applicant, "published": 1},
							fields=["name", "candidate_status", "published_on"]):
		events.append({"time": r.published_on, "kind": "Result", "title": _("Result published to candidate: {0}").format(_(r.candidate_status)),
					   "link": ["Grid Interview Result", r.name]})
	for ref in frappe.get_all("Grid Interview Reference", filters={"job_applicant": job_applicant},
							  fields=["name", "reference_person", "verification_status", "submitted_on", "verified_on"]):
		events.append({"time": ref.submitted_on, "kind": "Reference", "title": _("Reference added: {0}").format(ref.reference_person),
					   "link": ["Grid Interview Reference", ref.name]})
		if ref.verified_on:
			events.append({"time": ref.verified_on, "kind": "Reference",
						   "title": _("Reference {0}: {1}").format(ref.reference_person, _(ref.verification_status))})
	events = [e for e in events if e.get("time")]
	events.sort(key=lambda e: get_datetime(e["time"]))
	audit.log("candidate.timeline_viewed", "Job Applicant", job_applicant)
	return events


# --------------------------------------------------------------------------- final decision (HR)
DECISION_ROLES = {ADMIN_ROLE, HR_ROLE}


def record_decision(session_name: str, decision: str, summary: str = "", submit=0, override_missing_feedback=0) -> dict:
	"""HR records the round's outcome on the standard HRMS Interview (as the current user, so HRMS
	permissions and validations apply). Nothing is published to the candidate and the Job Applicant
	is not changed automatically."""
	user = frappe.session.user
	if user != "Administrator" and not DECISION_ROLES.intersection(frappe.get_roles(user)):
		raise frappe.PermissionError(_("Only HR can record interview decisions."))
	if decision not in ("Cleared", "Rejected", "Under Review"):
		frappe.throw(_("Invalid decision."))
	session = frappe.get_doc(SESSION, session_name)
	if session.status not in ("Completed", "No Show"):
		frappe.throw(_("Decisions can be recorded after the interview is completed."))
	interview = frappe.get_doc("Interview", session.interview)
	if interview.docstatus != 0:
		frappe.throw(_("This interview has already been submitted."))
	fb = _feedback_state([interview.name]).get(interview.name, {"assigned": [], "submitted": []})
	missing = sorted(set(fb["assigned"]) - set(fb["submitted"]))
	if missing and decision != "Under Review" and not cint(override_missing_feedback):
		frappe.throw(
			_("Feedback is still missing from: {0}. Wait for it or confirm the decision without it.").format(
				", ".join(frappe.utils.get_fullname(u) for u in missing)),
			title=_("Feedback pending"),
		)
	interview.status = decision
	if summary:
		interview.interview_summary = summary.strip()[:5000]
	interview.save()
	if cint(submit) and decision in ("Cleared", "Rejected"):
		interview.submit()  # HRMS then offers to update the Job Applicant; it is not done automatically
	audit.log("decision.recorded", "Interview", interview.name, decision=decision, submitted=bool(cint(submit)),
			  missing_feedback=len(missing))
	return {"interview": interview.name, "status": interview.status, "docstatus": interview.docstatus}
