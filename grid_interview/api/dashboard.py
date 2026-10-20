"""Dashboard, timeline, decision and retention endpoints (authorization in the services)."""

import frappe

from grid_interview.services import dashboard, retention


def _filters(filters):
	return frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})


@frappe.whitelist(methods=["GET"])
def get_summary(filters: str | dict | None = None):
	return dashboard.summary(_filters(filters))


@frappe.whitelist(methods=["GET"])
def get_list(bucket: str, filters: str | dict | None = None):
	return dashboard.bucket_rows(bucket, _filters(filters))


@frappe.whitelist(methods=["GET"])
def get_timeline(job_applicant: str):
	return dashboard.timeline(job_applicant)


@frappe.whitelist(methods=["POST"])
def record_decision(video_session: str, decision: str, summary: str = "", submit: int = 0,
					override_missing_feedback: int = 0):
	return dashboard.record_decision(video_session, decision, summary, submit, override_missing_feedback)


@frappe.whitelist(methods=["POST"])
def delete_recording(recording_name: str, reason: str):
	retention.admin_delete_recording(recording_name, reason)
	return {"ok": True}
