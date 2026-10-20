"""Consent-based recording: control, Jibri ingest, finalisation, storage and playback.

Control: only a moderator can start, only while the session is In Progress, recording is enabled
in settings and for this session, the concurrency and disk limits allow it, and every joined
participant has a current Recording consent. Jitsi's own record button is hidden, and any file
Jibri uploads without an authorised Grid Interview Recording is discarded.

Ingest (Jibri host -> this server): HMAC-signed, chunked upload (each chunk below the 50 MB web
server limit, each chunk and the whole file verified by SHA-256), then a background job
assembles, checks (size, checksum, ffprobe duration) and moves the file to storage.
"""

import hashlib
import hmac
import os
import shutil
import subprocess
import time

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime

from grid_interview.services import consent, jobs
from grid_interview.services.storage import get_storage
from grid_interview.utils import audit
from grid_interview.utils.settings import get_secret, get_settings

SESSION = "Grid Interview Video Session"
RECORDING = "Grid Interview Recording"
LIVE = ("Requested", "Recording")
SIGNATURE_WINDOW = 300  # seconds
ALLOWED_EXT = ("mp4", "webm", "mkv")


# --------------------------------------------------------------------------- control (moderator)
def _moderator_session(session_name: str):
	from grid_interview.services import meeting

	meeting.check_request_origin()
	session = frappe.get_doc(SESSION, session_name, for_update=True)
	participant = meeting.resolve_participant(session)
	if not participant.moderator:
		raise frappe.PermissionError(_("Only the interviewer can control recording."))
	return session, participant


def live_recording(session_name: str):
	return frappe.db.get_value(RECORDING, {"video_session": session_name, "status": ["in", list(LIVE)]}, "name")


def can_start(session) -> list[str]:
	"""Reasons recording cannot start now ([] = it can)."""
	s = get_settings()
	reasons = []
	if not cint(s.enable_recording):
		reasons.append(_("Recording is disabled in Grid Interview Settings."))
	if not cint(session.recording_eligible):
		reasons.append(_("This interview is not marked as eligible for recording."))
	if session.status != "In Progress":
		reasons.append(_("Recording can start once both the candidate and an interviewer have joined."))
	if live_recording(session.name):
		reasons.append(_("A recording is already running."))
	if frappe.db.count(RECORDING, {"status": ["in", list(LIVE)]}) >= cint(s.max_concurrent_recordings or 1):
		reasons.append(_("The maximum number of simultaneous recordings is reached. Try again later."))
	missing = consent.recording_blockers(session)
	if missing:
		reasons.append(_("Waiting for recording consent from: {0}").format(", ".join(missing)))
	if s.storage_backend != "S3":
		free_gb = get_storage().free_bytes() / 1024**3
		if free_gb < cint(s.min_free_disk_gb or 5):
			reasons.append(_("Not enough free disk space for a new recording."))
	return reasons


def start(session_name: str) -> dict:
	session, participant = _moderator_session(session_name)
	reasons = can_start(session)
	if reasons:
		audit.log("recording.start_refused", SESSION, session.name, result="Denied", reasons=reasons)
		frappe.throw("<br>".join(reasons), title=_("Recording cannot start"))
	consent_rows = frappe.get_all(
		"Grid Interview Consent",
		filters={"video_session": session.name, "consent_type": consent.RECORDING, "status": "Granted"},
		pluck="name",
	)
	rec = frappe.get_doc(
		{
			"doctype": RECORDING,
			"video_session": session.name,
			"interview": session.interview,
			"status": "Recording",
			"requested_by": participant.identity,
			"started_on": now_datetime(),
			"storage_backend": get_settings().storage_backend,
			"consents": ", ".join(consent_rows),
		}
	).insert(ignore_permissions=True)
	session.recording_status = "Recording"
	session.save(ignore_permissions=True)
	audit.log("recording.started", RECORDING, rec.name, session=session.name)
	return {"recording": rec.name, "max_minutes": cint(get_settings().max_recording_minutes or 120)}


def stop(session_name: str) -> dict:
	session, _participant = _moderator_session(session_name)
	name = live_recording(session.name)
	if name:
		frappe.db.set_value(RECORDING, name, {"status": "Finalizing", "ended_on": now_datetime()})
		audit.log("recording.stopped", RECORDING, name)
	session.recording_status = "Stopped"
	session.save(ignore_permissions=True)
	return {"recording_status": session.recording_status}


def on_consent_withdrawn(session_name: str):
	"""A participant withdrew recording consent: stop now, suspend (or delete) per policy."""
	policy = get_settings().on_consent_withdrawal or "Suspend"
	for name in frappe.get_all(RECORDING, filters={"video_session": session_name,
												   "status": ["not in", ["Deleted", "Failed"]]}, pluck="name"):
		rec = frappe.get_doc(RECORDING, name)
		if policy == "Delete" and rec.storage_key:
			_delete_file(rec)
		else:
			rec.status = "Suspended"
			rec.ended_on = rec.ended_on or now_datetime()
			rec.save(ignore_permissions=True)
		audit.log("recording.consent_withdrawn", RECORDING, name, policy=policy)
	frappe.db.set_value(SESSION, session_name, "recording_status", "Stopped")


def _delete_file(rec):
	try:
		get_storage(rec.storage_backend).delete(rec.storage_key)
	except Exception:
		frappe.log_error(title=f"grid_interview could not delete recording file {rec.name}")
		raise
	rec.status, rec.deleted_on, rec.storage_key = "Deleted", now_datetime(), None
	rec.save(ignore_permissions=True)
	audit.log("recording.deleted", RECORDING, rec.name)


def room_state(session) -> dict:
	"""Recording info for the room's heartbeat (never includes storage details)."""
	return {"recording_status": session.recording_status or "Not Requested", "recording_live": bool(live_recording(session.name))}


# --------------------------------------------------------------------------- ingest (Jibri, HMAC)
def canonical(params: dict) -> str:
	return "&".join(f"{k}={params[k]}" for k in sorted(params) if k not in ("cmd", "chunk"))


def sign(secret: str, timestamp: str, method: str, params: dict) -> str:
	message = f"{timestamp}\n{method}\n{canonical(params)}".encode()
	return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(method: str, params: dict, timestamp: str | None, signature: str | None):
	secret = get_secret("recording_webhook_secret")
	if not secret:
		raise frappe.PermissionError(_("Recording ingest is not configured."))
	try:
		ts = int(timestamp or "")
	except ValueError:
		raise frappe.PermissionError(_("Invalid signature."))
	# time.time() is the real epoch; now_datetime() is a naive site-time-zone datetime whose
	# .timestamp() would be interpreted in the container's zone (off by hours on an IST site).
	if abs(int(time.time()) - ts) > SIGNATURE_WINDOW:
		raise frappe.PermissionError(_("Signature expired."))
	expected = sign(secret, str(ts), method, params)
	if not signature or not hmac.compare_digest(expected, signature):
		audit.log("recording.ingest_bad_signature", actor="Guest", result="Denied", method=method)
		raise frappe.PermissionError(_("Invalid signature."))
	replay_key = f"grid_interview:ingest_sig:{signature}"
	if frappe.cache.get_value(replay_key):
		raise frappe.PermissionError(_("Replayed request."))
	frappe.cache.set_value(replay_key, 1, expires_in_sec=SIGNATURE_WINDOW * 2)


def ingest_dir(recording: str) -> str:
	return frappe.get_site_path("private", "grid_interview_ingest", recording)


def ingest_start(room: str, filename: str, size: int, sha256: str) -> dict:
	s = get_settings()
	session = frappe.db.get_value(SESSION, {"room_name": room}, "name")
	rec_name = frappe.db.get_value(
		RECORDING,
		{"video_session": session, "status": ["in", ["Recording", "Finalizing", "Suspended"]]},
		"name", order_by="creation desc",
	) if session else None
	if not rec_name:
		audit.log("recording.unauthorized_discarded", SESSION, session, actor="Guest", result="Denied", room=room)
		return {"accept": False, "reason": "no authorised recording for this room"}
	rec = frappe.get_doc(RECORDING, rec_name, for_update=True)
	if rec.status == "Suspended" and (s.on_consent_withdrawal or "Suspend") == "Delete":
		audit.log("recording.discarded_after_withdrawal", RECORDING, rec.name, actor="Guest")
		return {"accept": False, "reason": "consent withdrawn"}
	ext = (filename or "").rsplit(".", 1)[-1].lower()
	size = cint(size)
	if ext not in ALLOWED_EXT or size <= 0 or size > cint(s.max_recording_size_mb or 2048) * 1024 * 1024:
		_fail(rec, f"rejected upload: extension {ext!r}, size {size}")
		return {"accept": False, "reason": "file type or size not allowed"}
	if not (sha256 and len(sha256) == 64):
		return {"accept": False, "reason": "sha256 required"}
	provider_id = f"{room}/{os.path.basename(filename)}"[:140]
	duplicate = frappe.db.get_value(RECORDING, {"provider_recording_id": provider_id, "name": ["!=", rec.name]},
									["name", "status"], as_dict=True)
	if duplicate:
		# Jibri retried or re-sent a file we already have: answer idempotently instead of failing
		audit.log("recording.duplicate_upload", RECORDING, duplicate.name, actor="Guest", status=duplicate.status)
		return {"accept": False, "reason": f"already received as {duplicate.name}"}
	if s.storage_backend != "S3" and get_storage().free_bytes() < size * 2 + cint(s.min_free_disk_gb or 5) * 1024**3:
		_fail(rec, "not enough disk space to receive the recording")
		return {"accept": False, "reason": "insufficient disk space"}
	shutil.rmtree(ingest_dir(rec.name), ignore_errors=True)
	os.makedirs(ingest_dir(rec.name), mode=0o750, exist_ok=True)
	if rec.status != "Suspended":
		rec.status = "Uploading"
	rec.provider_recording_id = provider_id
	rec.expected_size_bytes, rec.expected_sha256 = size, sha256.lower()
	rec.ended_on = rec.ended_on or now_datetime()
	rec.save(ignore_permissions=True)
	frappe.db.set_value(SESSION, rec.video_session, "recording_status", "Stopped")
	audit.log("recording.upload_started", RECORDING, rec.name, actor="Guest", size=size)
	return {"accept": True, "recording": rec.name, "chunk_bytes": cint(s.ingest_chunk_mb or 20) * 1024 * 1024}


def ingest_chunk(recording: str, index, chunk_sha256: str, content: bytes) -> dict:
	rec = frappe.db.get_value(RECORDING, recording, ["name", "status", "expected_sha256"], as_dict=True)
	if not rec or rec.status not in ("Uploading", "Suspended") or not rec.expected_sha256:
		raise frappe.PermissionError(_("No upload in progress for this recording."))
	index = cint(index)
	limit = cint(get_settings().ingest_chunk_mb or 20) * 1024 * 1024
	if not 0 <= index < 100000 or not content or len(content) > limit:
		frappe.throw(_("Invalid chunk."))
	if hashlib.sha256(content).hexdigest() != (chunk_sha256 or "").lower():
		frappe.throw(_("Chunk checksum mismatch."))
	path = os.path.join(ingest_dir(recording), f"{index:05d}.part")
	with open(path + ".tmp", "wb") as fh:
		fh.write(content)
	os.replace(path + ".tmp", path)  # re-sending the same chunk is harmless
	return {"ok": True, "index": index}


def ingest_complete(recording: str, chunks) -> dict:
	rec = frappe.get_doc(RECORDING, recording, for_update=True)
	if rec.status not in ("Uploading", "Suspended"):
		raise frappe.PermissionError(_("No upload in progress for this recording."))
	chunks = cint(chunks)
	missing = [i for i in range(chunks) if not os.path.exists(os.path.join(ingest_dir(recording), f"{i:05d}.part"))]
	if chunks <= 0 or missing:
		frappe.throw(_("Missing chunks: {0}").format(missing[:20]))
	rec.upload_chunks = chunks
	rec.save(ignore_permissions=True)
	job = jobs.submit("Recording Finalize", RECORDING, recording)
	return {"ok": True, "job": job}


def _fail(rec, error: str):
	rec.status, rec.error_message = "Failed", error[:500]
	rec.save(ignore_permissions=True)
	audit.log("recording.failed", RECORDING, rec.name, actor="Administrator", error=error[:200])


# --------------------------------------------------------------------------- finalize (background job)
def probe_duration(path: str) -> float:
	try:
		result = subprocess.run(
			["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
			capture_output=True, text=True, timeout=120,
		)
	except FileNotFoundError:
		raise jobs.PermanentError("ffprobe is not installed on the server (install ffmpeg in the image).")
	if result.returncode != 0 or not result.stdout.strip():
		raise jobs.PermanentError("The uploaded file is not a readable video.")
	return flt(result.stdout.strip())


def finalize(recording: str):
	rec = frappe.get_doc(RECORDING, recording, for_update=True)
	if rec.status in ("Stored", "Deleted") or (rec.status == "Suspended" and rec.storage_key):
		return  # idempotent
	s = get_settings()
	folder = ingest_dir(recording)
	assembled = os.path.join(folder, "assembled.bin")
	digest, size = hashlib.sha256(), 0
	with open(assembled, "wb") as out:
		for i in range(cint(rec.upload_chunks)):
			with open(os.path.join(folder, f"{i:05d}.part"), "rb") as part:
				while block := part.read(1024 * 1024):
					digest.update(block)
					size += len(block)
					out.write(block)
	if size != cint(rec.expected_size_bytes) or digest.hexdigest() != rec.expected_sha256:
		raise jobs.PermanentError("Upload integrity check failed (size or SHA-256 mismatch)")
	duration = probe_duration(assembled)
	if duration > cint(s.max_recording_minutes or 120) * 60 + 300:
		raise jobs.PermanentError(f"Recording longer than the configured maximum ({duration:.0f}s)")
	ext = (rec.provider_recording_id or "x.mp4").rsplit(".", 1)[-1].lower()
	key = f"{getdate().strftime('%Y/%m')}/{rec.name}.{ext if ext in ALLOWED_EXT else 'mp4'}"
	storage = get_storage()
	stored = storage.put(assembled, key, "video/mp4" if ext == "mp4" else f"video/{ext}")
	if stored != size:
		raise OSError("stored size differs from the verified upload")
	days = cint(s.retention_recording_days)
	rec.update(
		{
			"storage_backend": storage.name,
			"storage_key": key,
			"size_bytes": size,
			"sha256": digest.hexdigest(),
			"duration_seconds": int(duration),
			"mime_type": "video/mp4" if ext == "mp4" else f"video/{ext}",
			"retention_until": add_days(getdate(), days) if days else None,
			"error_message": None,
		}
	)
	if rec.status != "Suspended":
		rec.status = "Stored"
	rec.save(ignore_permissions=True)
	shutil.rmtree(folder, ignore_errors=True)
	audit.log("recording.stored", RECORDING, rec.name, actor="Administrator", size=size, duration=int(duration))
	if rec.status == "Stored":
		from grid_interview.services import transcription

		transcription.queue_for(rec)


def on_job_failed(recording: str, error: str):
	"""Called by the job engine after the last attempt (after rolling back the handler's work)."""
	rec = frappe.get_doc(RECORDING, recording)
	if rec.status not in ("Stored", "Deleted", "Suspended"):
		_fail(rec, error)
	frappe.db.set_value(SESSION, rec.video_session, "processing_status", "Failed")


# --------------------------------------------------------------------------- playback
def can_view(rec, user=None) -> bool:
	from grid_interview.integrations import hrms_adapter
	from grid_interview.permissions import has_full_access

	user = user or frappe.session.user
	if has_full_access(user):
		return True
	return bool(rec.interview) and hrms_adapter.is_interviewer(user, rec.interview)


def stream(recording: str, as_attachment=False):
	from werkzeug.utils import redirect, send_file

	rec = frappe.get_doc(RECORDING, recording)
	if not can_view(rec):
		audit.log("recording.access_denied", RECORDING, recording, result="Denied")
		raise frappe.PermissionError(_("You may not view this recording."))
	if rec.status != "Stored" or not rec.storage_key:
		frappe.throw(_("This recording is not available ({0}).").format(_(rec.status)))
	audit.log("recording.downloaded" if as_attachment else "recording.played", RECORDING, recording)
	storage = get_storage(rec.storage_backend)
	filename = f"interview-{rec.name}.{rec.storage_key.rsplit('.', 1)[-1]}"
	if storage.name == "S3":
		return redirect(storage.presigned_url(rec.storage_key, filename, bool(as_attachment)), code=302)
	response = send_file(
		storage.path(rec.storage_key), environ=frappe.request.environ, mimetype=rec.mime_type or "video/mp4",
		as_attachment=bool(as_attachment), download_name=filename, conditional=True,  # Range requests for seeking
	)
	response.headers["Cache-Control"] = "private, no-store"
	response.headers["X-Content-Type-Options"] = "nosniff"
	return response
