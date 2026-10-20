"""Retention enforcement (daily) and authorised deletion.

Periods come from Grid Interview Settings › Retention (0 = keep forever):
  recordings     -> file deleted from storage, row kept as status Deleted (audit trail)
  transcripts    -> text and segments erased, status Deleted
  AI analyses    -> report content erased, status Deleted
  access sessions, notification logs, audit logs -> rows deleted
Suspended recordings (consent withdrawn, legal hold) are never deleted automatically.
Every run writes one audit row with counts; nothing here logs personal content.

Note on "secure" deletion: files are removed from the filesystem / S3. On EBS (and SSDs in
general) block-level overwrite is not guaranteed; rely on volume encryption at rest and, for S3,
a bucket without versioning (or a lifecycle rule that expires old versions).
"""

import os
import shutil
import time

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, now_datetime

from grid_interview.services.storage import get_storage
from grid_interview.utils import audit
from grid_interview.utils.settings import get_settings

RECORDING = "Grid Interview Recording"
TRANSCRIPT = "Grid Interview Transcript"
ANALYSIS = "Grid Interview Analysis"
BATCH = 500


def run(today=None) -> dict:
	"""Scheduler entry point."""
	today = getdate(today)
	s = get_settings()
	counts = {
		"recordings": _expire_recordings(today),
		"transcripts": _erase(TRANSCRIPT, today, {"full_text": None}, child="segments"),
		"analyses": _erase(ANALYSIS, today, {"summary": None, "report_json": None, "uncertainty_notes": None,
											 "provisional_recommendation": None, "review_notes": None}, child="findings"),
		"access_sessions": _delete_older("Grid Interview Access Session", s.retention_access_session_days, today),
		"notification_logs": _delete_older("Grid Interview Notification Log", s.retention_notification_log_days, today),
		"audit_logs": _delete_older("Grid Interview Audit Log", s.retention_audit_log_days, today),
		"stale_uploads": _clean_ingest(),
		"stuck_recordings": _fail_stuck_recordings(),
	}
	if any(counts.values()):
		audit.log("retention.run", actor="Administrator", **counts)
	return counts


def _expire_recordings(today) -> int:
	n = 0
	for name in frappe.get_all(RECORDING, filters={"retention_until": ["<", today],
												   "status": ["in", ["Stored", "Failed"]]}, pluck="name", limit=BATCH):
		try:
			delete_recording_file(name, reason="retention period ended", actor="Administrator")
			n += 1
		except Exception:
			frappe.log_error(title=f"grid_interview retention could not delete recording {name}")
	return n


def delete_recording_file(name: str, reason: str, actor: str | None = None):
	rec = frappe.get_doc(RECORDING, name)
	if rec.storage_key:
		get_storage(rec.storage_backend).delete(rec.storage_key)
	rec.update({"status": "Deleted", "deleted_on": now_datetime(), "storage_key": None, "sha256": None})
	rec.save(ignore_permissions=True)
	# derived data goes with the recording
	for t in frappe.get_all(TRANSCRIPT, filters={"recording": name, "status": ["!=", "Deleted"]}, pluck="name"):
		_erase_one(TRANSCRIPT, t, {"full_text": None}, "segments")
		for a in frappe.get_all(ANALYSIS, filters={"transcript": t, "status": ["!=", "Deleted"]}, pluck="name"):
			_erase_one(ANALYSIS, a, {"summary": None, "report_json": None, "uncertainty_notes": None,
									 "provisional_recommendation": None, "review_notes": None}, "findings")
	audit.log("recording.deleted", RECORDING, name, actor=actor, reason=reason)


def _erase_one(doctype, name, fields: dict, child: str):
	doc = frappe.get_doc(doctype, name)
	doc.update(fields)
	doc.set(child, [])
	doc.status = "Deleted"
	doc.flags.ignore_validate = True
	doc.save(ignore_permissions=True)


def _erase(doctype, today, fields: dict, child: str) -> int:
	names = frappe.get_all(doctype, filters={"retention_until": ["<", today], "status": ["!=", "Deleted"]},
						   pluck="name", limit=BATCH)
	for name in names:
		_erase_one(doctype, name, fields, child)
	return len(names)


def _delete_older(doctype, days, today) -> int:
	days = cint(days)
	if not days:
		return 0
	cutoff = add_days(today, -days)
	names = frappe.get_all(doctype, filters={"creation": ["<", cutoff]}, pluck="name", limit=BATCH * 4)
	frappe.flags.in_grid_interview_retention = True
	try:
		for name in names:
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True, delete_permanently=True)
	finally:
		frappe.flags.in_grid_interview_retention = False
	return len(names)


def _clean_ingest(max_age_hours=48) -> int:
	root = frappe.get_site_path("private", "grid_interview_ingest")
	if not os.path.isdir(root):
		return 0
	n = 0
	for entry in os.scandir(root):
		if entry.is_dir() and time.time() - entry.stat().st_mtime > max_age_hours * 3600:
			shutil.rmtree(entry.path, ignore_errors=True)
			n += 1
	return n


def _fail_stuck_recordings(max_age_hours=24) -> int:
	"""Recordings that never received (or finished) their upload are closed as Failed."""
	from frappe.utils import add_to_date

	cutoff = add_to_date(now_datetime(), hours=-max_age_hours)
	names = frappe.get_all(RECORDING, filters={"status": ["in", ["Recording", "Finalizing", "Uploading", "Requested"]],
											   "modified": ["<", cutoff]}, pluck="name", limit=BATCH)
	for name in names:
		frappe.db.set_value(RECORDING, name, {"status": "Failed", "error_message": "No complete upload received within 24 hours"})
		audit.log("recording.failed", RECORDING, name, actor="Administrator", reason="upload never completed")
	return len(names)


def admin_delete_recording(name: str, reason: str) -> None:
	"""Authorised manual deletion (e.g. a data-erasure request), including suspended recordings."""
	if frappe.session.user != "Administrator" and "Grid Interview Administrator" not in frappe.get_roles():
		raise frappe.PermissionError(_("Only Grid Interview Administrators can delete recordings."))
	if not (reason or "").strip():
		frappe.throw(_("A reason is required."))
	delete_recording_file(name, reason=reason.strip()[:300])
