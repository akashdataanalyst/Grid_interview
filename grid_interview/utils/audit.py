"""Append-only audit trail for sensitive operations.

Metadata is sanitized: keys that look like secrets, tokens or transcript content are dropped,
and long values are truncated, so audit rows can be shown to administrators safely.
"""

import json
import re

import frappe

from grid_interview.utils.security import client_fingerprint

_SENSITIVE_KEY = re.compile(r"token|secret|password|passwd|api_?key|authorization|cookie|transcript|full_text|sid", re.I)
_MAX_VALUE_LEN = 300


def sanitize(metadata: dict | None) -> dict:
	clean = {}
	for key, value in (metadata or {}).items():
		if _SENSITIVE_KEY.search(str(key)):
			clean[key] = "[redacted]"
			continue
		if isinstance(value, (dict, list, tuple)):
			value = json.dumps(value, default=str)
		if isinstance(value, str) and len(value) > _MAX_VALUE_LEN:
			value = value[:_MAX_VALUE_LEN] + "…"
		clean[key] = value
	return clean


def log(action: str, reference_doctype=None, reference_name=None, result="Success", actor=None, **metadata):
	"""Write one audit row. Never raises: an audit failure is reported to the Error Log instead.

	"Denied" rows are committed at once: the caller raises right after, and the request's rollback
	would otherwise discard the record of the refusal (and, for join links, the failed-attempt count
	saved just before). Refusals therefore happen before a request writes anything else."""
	try:
		row = frappe.get_doc(
			{
				"doctype": "Grid Interview Audit Log",
				"actor": actor or frappe.session.user,
				"action": action,
				"result": result,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"client_fingerprint": client_fingerprint(),
				"metadata": json.dumps(sanitize(metadata), default=str, sort_keys=True) if metadata else None,
			}
		)
		# An audit row must be written even when the referenced record does not (or no longer) exist,
		# e.g. a rejected request for an unknown id or a record removed by retention.
		row.flags.ignore_links = True
		row.insert(ignore_permissions=True)
		request = getattr(frappe.local, "request", None)
		if request is not None and result == "Denied" and not frappe.in_test:
			frappe.db.commit()  # nosemgrep
		elif request is not None and request.method not in ("POST", "PUT", "PATCH", "DELETE"):
			# Frappe commits GET requests only when asked to; without this, audit rows written
			# during GETs (document downloads, page views) would be silently discarded.
			frappe.local.flags.commit = True
	except Exception:
		frappe.log_error(title=f"grid_interview audit log failed: {action}")
