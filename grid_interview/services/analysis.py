"""AI draft interview reports (decision support only).

* Input: the transcript (timestamps), the job title, the round and its expected skills (the
  rubric from the HRMS Interview Type). Nothing else about the candidate is sent.
* Output must be a JSON object that passes `validate()` on the server: wrong types, unknown
  enum values, out-of-range timestamps or any reference to protected / non-job-related traits
  reject the report. "Transcript Fact" evidence that is not found in the transcript is
  downgraded to "Interpretation" with a warning.
* Every report is a Draft awaiting human review. Reviewers approve, correct, reject or
  regenerate it. An analysis never changes the HRMS Interview, never creates Interview Feedback
  and never becomes a candidate-facing Result.
"""

import json
import re
import time

import requests

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime

from grid_interview.integrations import hrms_adapter
from grid_interview.services import jobs
from grid_interview.utils import audit
from grid_interview.utils.settings import get_secret, get_settings

TRANSCRIPT = "Grid Interview Transcript"
ANALYSIS = "Grid Interview Analysis"
SESSION = "Grid Interview Video Session"
EXTERNAL = "OpenAI Compatible"
MOCK = "Mock (testing only)"
PROMPT_VERSION = "gi-analysis-v1"
REVIEW_ROLES = {"Grid Interview Reviewer", "Grid Interview Administrator"}

RATINGS = ("Demonstrated", "Partially demonstrated", "Not demonstrated", "Not assessed")
BASES = ("Transcript Fact", "Interpretation", "Missing Evidence")
RECOMMENDATIONS = ("Proceed", "Hold", "Do Not Proceed", "Insufficient Evidence")

# Non-job-related or protected characteristics the report must never assess (English + common Hindi).
# Phrases, not bare words: "race condition", "disable the service", "health and safety",
# "UI appearance" or "accent colour" are legitimate in job interviews and must not be rejected.
BANNED = re.compile(
	r"\b((his|her|their|the candidate'?s) accent|speaking accent|gender|religio(n|us)|caste|ethnicity|"
	r"ethnic (group|background|origin)|racial|skin colou?r|nationality|marital|married|pregnan\w*|"
	r"physical appearance|good[- ]looking|disabilit\w*|medical condition|health condition|facial expression\w*|"
	r"body language|emotional state|personality type|introvert\w*|extrovert\w*|years old|how old|"
	r"umar|jaati|dharm|shaadi)\b",
	re.I,
)

SYSTEM_PROMPT = """You draft a structured, evidence-based interview report for a human hiring panel.
Rules:
1. Use ONLY the transcript provided. Never invent questions, answers, facts or skills.
2. Assess only the job and the listed skills. Never mention, infer or assess gender, age, religion,
   caste, ethnicity, nationality, accent, language fluency beyond job requirements, appearance,
   disability, health, marital or family status, emotions, facial expressions, body language or personality.
3. Quote evidence verbatim from the transcript and give its start time in seconds.
4. For every finding set "basis": "Transcript Fact" (directly stated), "Interpretation" (a reasonable
   inference; say why) or "Missing Evidence" (the topic was not covered).
5. Speaker labels may be missing. If you cannot tell who said something, add that to "uncertainty".
6. You do not decide. "provisional_recommendation" is only a suggestion for the panel; use
   "Insufficient Evidence" when the transcript is short, unclear or does not cover the skills.
Reply with ONE JSON object and nothing else, with exactly these keys:
{"summary": str,
 "questions_answers": [{"question": str, "answer_summary": str, "start_seconds": number}],
 "skills": [{"skill": str, "rating": "Demonstrated"|"Partially demonstrated"|"Not demonstrated"|"Not assessed",
             "evidence": str, "start_seconds": number|null, "basis": "Transcript Fact"|"Interpretation"|"Missing Evidence"}],
 "strengths": [{"claim": str, "evidence": str, "start_seconds": number|null, "basis": str}],
 "gaps": [{"claim": str, "evidence": str, "start_seconds": number|null, "basis": str}],
 "clarifications": [str],
 "follow_up_questions": [str],
 "uncertainty": [str],
 "provisional_recommendation": "Proceed"|"Hold"|"Do Not Proceed"|"Insufficient Evidence"}"""


# --------------------------------------------------------------------------- queueing
def queue_for(transcript) -> str | None:
	s = get_settings()
	provider = s.ai_provider or "Disabled"
	if provider == "Disabled":
		frappe.db.set_value(SESSION, transcript.video_session, "processing_status", "Completed")
		return None
	if provider == EXTERNAL and not cint(s.enable_external_processing):
		frappe.db.set_value(SESSION, transcript.video_session, "processing_status", "Completed")
		return None
	name = frappe.db.get_value(ANALYSIS, {"transcript": transcript.name}, "name")
	version = cint(frappe.db.get_value(ANALYSIS, name, "report_version")) if name else 1
	if not name:
		frappe.get_doc(
			{"doctype": ANALYSIS, "interview": transcript.interview, "transcript": transcript.name,
			 "status": "Queued", "review_status": "Pending Review", "report_version": 1,
			 "prompt_version": PROMPT_VERSION}
		).insert(ignore_permissions=True)
	return jobs.submit("Analysis", TRANSCRIPT, transcript.name, key=f"Analysis:{transcript.name}:v{version}")


# --------------------------------------------------------------------------- job
def analyze(transcript_name: str):
	t = frappe.get_doc(TRANSCRIPT, transcript_name)
	if t.status != "Completed":
		raise jobs.PermanentError("Transcript is not completed")
	a = frappe.get_doc(ANALYSIS, frappe.db.get_value(ANALYSIS, {"transcript": t.name}, "name"), for_update=True)
	if a.status == "Draft":
		return  # this version is already done (idempotent)
	s = get_settings()
	info = hrms_adapter.get_interview(t.interview)
	skills = [row["skill"] for row in hrms_adapter.get_expected_skills(info.interview_type)]
	segments = [{"start": flt(x.start_seconds), "end": flt(x.end_seconds), "text": x.text} for x in t.segments]
	a.update({"status": "Processing", "provider": s.ai_provider, "error_message": None})
	a.save(ignore_permissions=True)

	started = time.monotonic()
	if s.ai_provider == MOCK:
		raw, model = mock_report(skills, segments), "mock"
	elif s.ai_provider == EXTERNAL:
		raw, model = _call_llm(build_messages(info, skills, segments, cint(s.ai_max_transcript_chars or 120000)), s)
	else:
		raise jobs.PermanentError("AI analysis is disabled")
	report, warnings = validate(raw, segments, skills)  # ValueError -> retried (model output varies)

	days = cint(s.retention_analysis_days)
	a.set("findings", [])
	for row in _findings(report):
		a.append("findings", row)
	a.update(
		{
			"status": "Draft",
			"review_status": "Pending Review",
			"reviewed_by": None,
			"reviewed_on": None,
			"model": model,
			"prompt_version": PROMPT_VERSION,
			"generated_on": now_datetime(),
			"processing_ms": int((time.monotonic() - started) * 1000),
			"summary": report["summary"],
			"provisional_recommendation": report["provisional_recommendation"],
			"uncertainty_notes": "\n".join(report["uncertainty"] + warnings),
			"report_json": json.dumps(report, ensure_ascii=False, indent=1),
			"retention_until": add_days(getdate(), days) if days else None,
		}
	)
	a.save(ignore_permissions=True)
	frappe.db.set_value(SESSION, t.video_session, "processing_status", "Completed")
	audit.log("analysis.generated", ANALYSIS, a.name, actor="Administrator", version=a.report_version, model=model)
	notify_reviewers(a, info)


def on_job_failed(transcript: str, error: str):
	name = frappe.db.get_value(ANALYSIS, {"transcript": transcript}, "name")
	if name:
		frappe.db.set_value(ANALYSIS, name, {"status": "Failed", "error_message": error[:500]})
	session = frappe.db.get_value(TRANSCRIPT, transcript, "video_session")
	frappe.db.set_value(SESSION, session, "processing_status", "Failed")
	audit.log("analysis.failed", TRANSCRIPT, transcript, actor="Administrator")


# --------------------------------------------------------------------------- prompt / providers
def _ts(seconds: float) -> str:
	seconds = int(seconds)
	return f"{seconds // 60:02d}:{seconds % 60:02d}"


def build_messages(info, skills: list[str], segments: list[dict], max_chars: int) -> list[dict]:
	lines, used, truncated = [], 0, False
	for seg in segments:
		line = f"[{seg['start']:.0f}s {_ts(seg['start'])}] {seg['text']}"
		if used + len(line) > max_chars:
			truncated = True
			break
		lines.append(line)
		used += len(line) + 1
	user = "\n".join(
		[
			f"Job title: {info.job_title or info.designation or 'not specified'}",
			f"Interview round: {info.interview_type}",
			"Skills to assess (rubric): " + (", ".join(skills) if skills else "none specified; assess only clearly job-related answers"),
			"Speaker labels are not available in this transcript." ,
			"Transcript (start time in seconds, then text):" + (" [TRUNCATED: only the first part is included]" if truncated else ""),
			*lines,
		]
	)
	return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _call_llm(messages: list[dict], s) -> tuple[str, str]:
	key = get_secret("ai_api_key")
	if not key:
		raise jobs.PermanentError("AI API key is not configured")
	url = f"{(s.ai_api_base or 'https://api.openai.com/v1').rstrip('/')}/chat/completions"
	proxies = {"https": s.https_proxy} if s.https_proxy else None
	r = requests.post(
		url,
		headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
		json={"model": s.ai_model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}},
		timeout=cint(s.request_timeout_seconds or 300),
		proxies=proxies,
	)
	if r.status_code in (400, 401, 403, 404, 422):
		raise jobs.PermanentError(f"AI provider rejected the request (HTTP {r.status_code})")
	if r.status_code >= 300:
		raise RuntimeError(f"AI provider error (HTTP {r.status_code})")
	data = r.json()
	return data["choices"][0]["message"]["content"], data.get("model") or s.ai_model


def mock_report(skills: list[str], segments: list[dict]) -> str:
	first = segments[0] if segments else {"start": 0, "text": ""}
	return json.dumps(
		{
			"summary": "Mock analysis for testing. Not a real assessment.",
			"questions_answers": [{"question": "Opening question", "answer_summary": first["text"][:200],
								   "start_seconds": first["start"]}] if segments else [],
			"skills": [{"skill": sk, "rating": "Not assessed", "evidence": "", "start_seconds": None,
						"basis": "Missing Evidence"} for sk in skills],
			"strengths": [],
			"gaps": [],
			"clarifications": [],
			"follow_up_questions": ["Ask the candidate for a concrete example related to the role."],
			"uncertainty": ["Mock provider: generated without AI."],
			"provisional_recommendation": "Insufficient Evidence",
		}
	)


# --------------------------------------------------------------------------- validation
def _norm(text: str) -> str:
	return re.sub(r"[^\w]+", " ", (text or "").lower()).strip()


def _strip_fences(raw: str) -> str:
	raw = (raw or "").strip()
	if raw.startswith("```"):
		raw = re.sub(r"^```(json)?", "", raw).rstrip("`").strip()
	return raw


def validate(raw: str | dict, segments: list[dict], skills: list[str]) -> tuple[dict, list[str]]:
	"""Return (clean_report, warnings) or raise ValueError."""
	try:
		data = json.loads(_strip_fences(raw)) if isinstance(raw, str) else raw
	except (TypeError, json.JSONDecodeError) as e:
		raise ValueError(f"AI output is not valid JSON: {e}") from None
	if not isinstance(data, dict):
		raise ValueError("AI output must be a JSON object")
	errors, warnings = [], []
	duration = max([s["end"] for s in segments] or [0]) + 5
	transcript = _norm(" ".join(s["text"] for s in segments))

	def text(v, field, limit=4000, required=False):
		if v is None or v == "":
			if required:
				errors.append(f"{field} is required")
			return ""
		if not isinstance(v, str):
			errors.append(f"{field} must be text")
			return ""
		return v.strip()[:limit]

	def seconds(v, field):
		if v in (None, ""):
			return None
		if not isinstance(v, (int, float)) or not 0 <= v <= duration:
			errors.append(f"{field} is outside the transcript ({v})")
			return None
		return float(v)

	def items(key, limit=50):
		v = data.get(key, [])
		if not isinstance(v, list):
			errors.append(f"{key} must be a list")
			return []
		return v[:limit]

	def evidence_basis(item, field):
		basis = item.get("basis") if item.get("basis") in BASES else None
		if basis is None:
			errors.append(f"{field}.basis must be one of {BASES}")
			return "Interpretation"
		quote = text(item.get("evidence"), f"{field}.evidence", 1000)
		if basis == "Transcript Fact" and (not quote or _norm(quote) not in transcript):
			warnings.append(f"{field}: evidence quote not found verbatim in the transcript; treated as interpretation.")
			basis = "Interpretation"
		return basis

	report = {"summary": text(data.get("summary"), "summary", 3000, required=True)}
	report["questions_answers"] = [
		{"question": text(q.get("question"), "question", 1000, True), "answer_summary": text(q.get("answer_summary"), "answer_summary", 2000),
		 "start_seconds": seconds(q.get("start_seconds"), "questions_answers.start_seconds")}
		for q in items("questions_answers") if isinstance(q, dict)
	]
	report["skills"] = []
	for i, sk in enumerate(items("skills")):
		if not isinstance(sk, dict):
			continue
		if sk.get("rating") not in RATINGS:
			errors.append(f"skills[{i}].rating must be one of {RATINGS}")
		report["skills"].append({"skill": text(sk.get("skill"), "skill", 140, True), "rating": sk.get("rating"),
								 "evidence": text(sk.get("evidence"), "skill.evidence", 1000),
								 "start_seconds": seconds(sk.get("start_seconds"), f"skills[{i}].start_seconds"),
								 "basis": evidence_basis(sk, f"skills[{i}]")})
	for key in ("strengths", "gaps"):
		report[key] = [
			{"claim": text(x.get("claim"), f"{key}.claim", 1000, True), "evidence": text(x.get("evidence"), f"{key}.evidence", 1000),
			 "start_seconds": seconds(x.get("start_seconds"), f"{key}.start_seconds"), "basis": evidence_basis(x, key)}
			for x in items(key) if isinstance(x, dict)
		]
	for key in ("clarifications", "follow_up_questions", "uncertainty"):
		report[key] = [text(x, key, 1000) for x in items(key, 30) if isinstance(x, str) and x.strip()]
	if data.get("provisional_recommendation") not in RECOMMENDATIONS:
		errors.append(f"provisional_recommendation must be one of {RECOMMENDATIONS}")
	report["provisional_recommendation"] = data.get("provisional_recommendation")

	if skills:
		unknown = {s["skill"] for s in report["skills"]} - set(skills)
		if unknown:
			warnings.append("Skills outside the rubric were included: " + ", ".join(sorted(unknown)))
	if not segments or duration < 60:
		if report["provisional_recommendation"] not in ("Insufficient Evidence", None):
			warnings.append("Very short transcript: recommendation set to Insufficient Evidence.")
			report["provisional_recommendation"] = "Insufficient Evidence"

	banned = BANNED.search(json.dumps(report, ensure_ascii=False))
	if banned:
		errors.append(f"output refers to a non-job-related characteristic ({banned.group(0)!r})")
	if errors:
		raise ValueError("AI output failed validation: " + "; ".join(errors[:10]))
	return report, warnings


def _findings(report: dict) -> list[dict]:
	rows = []
	for q in report["questions_answers"]:
		rows.append({"finding_type": "Question and Answer", "skill": "", "basis": "Transcript Fact",
					 "claim": f"Q: {q['question']}\nA: {q['answer_summary']}"[:2000], "evidence": "",
					 "evidence_start_seconds": q["start_seconds"]})
	for sk in report["skills"]:
		rows.append({"finding_type": "Skill", "skill": sk["skill"], "basis": sk["basis"],
					 "claim": sk["rating"], "evidence": sk["evidence"], "evidence_start_seconds": sk["start_seconds"]})
	for key, ftype in (("strengths", "Strength"), ("gaps", "Gap")):
		for x in report[key]:
			rows.append({"finding_type": ftype, "skill": "", "basis": x["basis"], "claim": x["claim"],
						 "evidence": x["evidence"], "evidence_start_seconds": x["start_seconds"]})
	for key, ftype in (("clarifications", "Clarification Needed"), ("follow_up_questions", "Follow-up Question"),
					   ("uncertainty", "Uncertainty")):
		for x in report[key]:
			rows.append({"finding_type": ftype, "skill": "", "basis": "Interpretation", "claim": x, "evidence": ""})
	return rows


# --------------------------------------------------------------------------- notifications
def notify_reviewers(a, info):
	from grid_interview.services import notifications

	recipients = set()
	for user in info.interviewers:
		if frappe.db.get_value("User", user, "enabled"):
			recipients.add(frappe.db.get_value("User", user, "email"))
	for user in frappe.get_all("Has Role", filters={"role": "Grid Interview Reviewer", "parenttype": "User"}, pluck="parent"):
		if frappe.db.get_value("User", user, "enabled"):
			recipients.add(frappe.db.get_value("User", user, "email"))
	for email in filter(None, recipients):
		notifications.notify("analysis_ready", a.name, email.lower(), "Interviewer",
							 f"analysis_ready:{a.name}:v{a.report_version}:{email.lower()}", reference_doctype=ANALYSIS)


# --------------------------------------------------------------------------- human review
def ensure_reviewer():
	if frappe.session.user != "Administrator" and not REVIEW_ROLES.intersection(frappe.get_roles()):
		raise frappe.PermissionError(_("Only reviewers can review AI reports."))


def review(name: str, decision: str, notes: str = "", corrected_summary: str | None = None) -> None:
	ensure_reviewer()
	if decision not in ("Approved", "Corrected", "Rejected"):
		frappe.throw(_("Invalid review decision."))
	a = frappe.get_doc(ANALYSIS, name)
	if a.status != "Draft":
		frappe.throw(_("Only a generated draft can be reviewed."))
	if decision in ("Corrected", "Rejected") and not (notes or "").strip():
		frappe.throw(_("Please explain the correction or rejection in the review notes."))
	if decision == "Corrected" and corrected_summary:
		a.summary = corrected_summary.strip()[:3000]
	a.update({"review_status": decision, "review_notes": (notes or "").strip()[:4000],
			  "reviewed_by": frappe.session.user, "reviewed_on": now_datetime()})
	a.save(ignore_permissions=True)
	audit.log(f"analysis.{decision.lower()}", ANALYSIS, name)


def regenerate(name: str) -> str:
	ensure_reviewer()
	a = frappe.get_doc(ANALYSIS, name)
	if a.status in ("Queued", "Processing"):
		frappe.throw(_("The report is already being generated."))
	a.report_version = cint(a.report_version) + 1
	a.status = "Queued"
	a.review_status = "Pending Review"
	a.save(ignore_permissions=True)
	audit.log("analysis.regenerate_requested", ANALYSIS, name, version=a.report_version)
	return jobs.submit("Analysis", TRANSCRIPT, a.transcript, key=f"Analysis:{a.transcript}:v{a.report_version}")
