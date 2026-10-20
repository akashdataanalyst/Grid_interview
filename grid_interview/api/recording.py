"""Consent, recording control, Jibri ingest, playback and review endpoints."""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from grid_interview.api.video import _access
from grid_interview.services import analysis, consent, jobs, meeting, recording

SESSION = "Grid Interview Video Session"


# --------------------------------------------------------------------------- room (participants)
@frappe.whitelist(allow_guest=True, methods=["GET"])
def consent_status(video_session: str):
	access = _access(video_session)
	session = frappe.get_doc(SESSION, video_session)
	participant = meeting.resolve_participant(session, access)
	data = consent.status(session, participant)
	data.update(recording.room_state(session))
	if participant.moderator:
		data["start_blockers"] = recording.can_start(session)
	return data


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60 * 60)
def set_consent(video_session: str, consent_type: str, granted: int):
	access = _access(video_session)
	meeting.check_request_origin(access)
	session = frappe.get_doc(SESSION, video_session)
	participant = meeting.resolve_participant(session, access)
	return consent.record(session, participant, consent_type, bool(int(granted)))


@frappe.whitelist(methods=["POST"])
def start_recording(video_session: str):
	return recording.start(video_session)


@frappe.whitelist(methods=["POST"])
def stop_recording(video_session: str):
	return recording.stop(video_session)


# --------------------------------------------------------------------------- Jibri ingest (HMAC)
def _verified(method: str, params: dict):
	recording.verify_signature(
		method, params, frappe.get_request_header("X-GI-Timestamp"), frappe.get_request_header("X-GI-Signature")
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60 * 60)
def ingest_start(room: str, filename: str, size: int, sha256: str):
	_verified("ingest_start", {"room": room, "filename": filename, "size": size, "sha256": sha256})
	return recording.ingest_start(room, filename, int(size), sha256)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=2000, seconds=60 * 60)
def ingest_chunk(recording_name: str, index: int, chunk_sha256: str):
	_verified("ingest_chunk", {"recording_name": recording_name, "index": index, "chunk_sha256": chunk_sha256})
	upload = frappe.request.files.get("chunk") if frappe.request else None
	if not upload:
		frappe.throw(_("Chunk file missing."))
	return recording.ingest_chunk(recording_name, index, chunk_sha256, upload.stream.read())


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60 * 60)
def ingest_complete(recording_name: str, chunks: int):
	_verified("ingest_complete", {"recording_name": recording_name, "chunks": chunks})
	return recording.ingest_complete(recording_name, chunks)


# --------------------------------------------------------------------------- playback / review
@frappe.whitelist(methods=["GET"])
def stream(recording_name: str, download: int = 0):
	return recording.stream(recording_name, as_attachment=bool(int(download)))


@frappe.whitelist(methods=["POST"])
def review_analysis(name: str, decision: str, notes: str = "", corrected_summary: str | None = None):
	analysis.review(name, decision, notes, corrected_summary)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def regenerate_analysis(name: str):
	return {"job": analysis.regenerate(name)}


@frappe.whitelist(methods=["POST"])
def retry_job(name: str):
	jobs.admin_retry(name)
	return {"ok": True}
