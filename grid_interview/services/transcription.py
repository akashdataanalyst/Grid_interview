"""Background transcription of stored recordings.

Providers: "OpenAI Compatible" (POST {base}/audio/transcriptions, verbose_json segments) and
"Mock (testing only)". External providers run only when Grid Interview Settings allows external
processing AND the candidate gave External Processing consent in the room.
Speaker attribution: Whisper-style APIs do not identify speakers, so every segment is marked
speaker_uncertain instead of guessing names. Hindi, English and mixed speech are left to the
provider's language detection unless a language hint is configured.
"""

import math
import os
import shutil
import subprocess
import tempfile

import requests

import frappe
from frappe.utils import add_days, cint, flt, getdate, now_datetime

from grid_interview.services import consent, jobs
from grid_interview.services.storage import get_storage
from grid_interview.utils import audit
from grid_interview.utils.settings import get_secret, get_settings

RECORDING = "Grid Interview Recording"
TRANSCRIPT = "Grid Interview Transcript"
SESSION = "Grid Interview Video Session"
EXTERNAL = "OpenAI Compatible"
MOCK = "Mock (testing only)"
SEGMENT_SECONDS = 600  # 10-minute audio parts (~2.4 MB at 32 kbit/s, far below API upload limits)


def queue_for(rec) -> str | None:
	"""Submit transcription for a stored recording if the configuration and consent allow it."""
	s = get_settings()
	provider = s.transcription_provider or "Disabled"
	if provider == "Disabled":
		return None
	session = frappe.get_doc(SESSION, rec.video_session)
	if provider == EXTERNAL:
		if not cint(s.enable_external_processing) or not consent.candidate_allows_external_processing(session):
			audit.log("transcription.skipped", RECORDING, rec.name, actor="Administrator",
					  reason="external processing not allowed or candidate consent missing")
			return None
	frappe.db.set_value(SESSION, session.name, "processing_status", "In Progress")
	return jobs.submit("Transcription", RECORDING, rec.name)


def transcribe(recording: str):
	rec = frappe.get_doc(RECORDING, recording)
	if rec.status != "Stored" or not rec.storage_key:
		raise jobs.PermanentError(f"Recording is {rec.status}; nothing to transcribe")
	done = frappe.db.get_value(TRANSCRIPT, {"recording": recording, "status": "Completed"}, "name")
	if done:
		_next(frappe.get_doc(TRANSCRIPT, done))  # idempotent: just make sure analysis is queued
		return
	s = get_settings()
	provider = s.transcription_provider
	name = frappe.db.get_value(TRANSCRIPT, {"recording": recording}, "name")
	t = frappe.get_doc(TRANSCRIPT, name) if name else frappe.new_doc(TRANSCRIPT)
	t.update({"recording": recording, "video_session": rec.video_session, "interview": rec.interview,
			  "status": "Processing", "provider": provider, "started_on": now_datetime(), "error_message": None})
	t.save(ignore_permissions=True) if name else t.insert(ignore_permissions=True)

	if provider == MOCK:
		segments, language, model = _mock_segments(cint(rec.duration_seconds) or 60), "en", "mock"
	elif provider == EXTERNAL:
		segments, language, model = _external(rec, s)
	else:
		raise jobs.PermanentError("Transcription is disabled")

	days = cint(s.retention_transcript_days)
	t.set("segments", [])
	for seg in segments:
		t.append("segments", seg)
	t.update(
		{
			"status": "Completed",
			"language": language,
			"model": model,
			"full_text": "\n".join(seg["text"] for seg in segments),
			"completed_on": now_datetime(),
			"retention_until": add_days(getdate(), days) if days else None,
		}
	)
	t.save(ignore_permissions=True)
	audit.log("transcript.generated", TRANSCRIPT, t.name, actor="Administrator", segments=len(segments),
			  provider=provider)  # never the text itself
	_next(t)


def _next(t):
	from grid_interview.services import analysis

	analysis.queue_for(t)


def _mock_segments(duration: int) -> list[dict]:
	third = max(duration / 3, 1)
	lines = [
		"Mock transcript: the interviewer introduces the role.",
		"Mock transcript: the candidate describes a past project.",
		"Mock transcript: closing questions.",
	]
	return [
		{"start_seconds": round(i * third, 2), "end_seconds": round((i + 1) * third, 2), "speaker_label": "",
		 "speaker_uncertain": 1, "confidence": None, "text": line}
		for i, line in enumerate(lines)
	]


def extract_audio(src: str) -> tuple[str, list[str]]:
	out_dir = tempfile.mkdtemp(prefix="gi-audio-")
	cmd = ["ffmpeg", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
		   "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), os.path.join(out_dir, "part%03d.mp3")]
	try:
		result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
	except FileNotFoundError:
		shutil.rmtree(out_dir, ignore_errors=True)
		raise jobs.PermanentError("ffmpeg is not installed on the server (install ffmpeg in the image).")
	parts = sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".mp3"))
	if result.returncode != 0 or not parts:
		shutil.rmtree(out_dir, ignore_errors=True)
		raise jobs.PermanentError("Could not extract audio from the recording")
	return out_dir, parts


def _external(rec, s) -> tuple[list[dict], str, str]:
	key = get_secret("transcription_api_key")
	if not key:
		raise jobs.PermanentError("Transcription API key is not configured")
	src, temporary = get_storage(rec.storage_backend).local_copy(rec.storage_key)
	out_dir = None
	try:
		out_dir, parts = extract_audio(src)
		segments, language = [], None
		for index, part in enumerate(parts):
			data = _call_api(part, s, key)
			language = language or data.get("language")
			offset = index * SEGMENT_SECONDS
			for seg in data.get("segments") or []:
				text = (seg.get("text") or "").strip()
				if not text:
					continue
				logprob = seg.get("avg_logprob")
				segments.append(
					{
						"start_seconds": round(offset + flt(seg.get("start")), 2),
						"end_seconds": round(offset + flt(seg.get("end")), 2),
						"speaker_label": "",
						"speaker_uncertain": 1,  # the API does not identify speakers
						"confidence": round(min(1.0, max(0.0, math.exp(logprob))), 3) if logprob is not None else None,
						"text": text[:2000],
					}
				)
			if not data.get("segments") and (data.get("text") or "").strip():
				segments.append({"start_seconds": offset, "end_seconds": offset + SEGMENT_SECONDS, "speaker_label": "",
								 "speaker_uncertain": 1, "confidence": None, "text": data["text"].strip()[:2000]})
		return segments, language or (s.transcription_language or ""), s.transcription_model
	finally:
		if out_dir:
			shutil.rmtree(out_dir, ignore_errors=True)
		if temporary:
			os.remove(src)


def _call_api(path: str, s, key: str) -> dict:
	url = f"{(s.transcription_api_base or 'https://api.openai.com/v1').rstrip('/')}/audio/transcriptions"
	data = [("model", s.transcription_model or "whisper-1"), ("response_format", "verbose_json"),
			("timestamp_granularities[]", "segment")]
	if s.transcription_language:
		data.append(("language", s.transcription_language))
	proxies = {"https": s.https_proxy} if s.https_proxy else None
	with open(path, "rb") as fh:
		r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, data=data,
						  files={"file": (os.path.basename(path), fh, "audio/mpeg")},
						  timeout=cint(s.request_timeout_seconds or 300), proxies=proxies)
	if r.status_code in (400, 401, 403, 404, 413, 422):
		raise jobs.PermanentError(f"Transcription provider rejected the request (HTTP {r.status_code})")
	if r.status_code >= 300:
		raise RuntimeError(f"Transcription provider error (HTTP {r.status_code})")  # retried
	return r.json()


def on_job_failed(recording: str, error: str):
	name = frappe.db.get_value(TRANSCRIPT, {"recording": recording}, "name")
	if name:
		frappe.db.set_value(TRANSCRIPT, name, {"status": "Failed", "error_message": error[:500]})
	session = frappe.db.get_value(RECORDING, recording, "video_session")
	frappe.db.set_value(SESSION, session, "processing_status", "Failed")
	audit.log("transcript.failed", RECORDING, recording, actor="Administrator")
