"""Guard rails over every whitelisted endpoint of the app (fails when a new endpoint is added
without a deliberate decision about guest access and HTTP methods)."""

import importlib
import inspect
import pkgutil

import frappe

import grid_interview.api as api_pkg
from grid_interview.tests.utils import RollbackTestCase

# Every guest-callable endpoint, and why it is safe without a session.
GUEST_ALLOWED = {
	"grid_interview.api.portal.register": "rate limited, generic response, setting-gated",
	"grid_interview.api.portal.submit_reference_response": "single-use hashed token, rate limited",
	"grid_interview.api.video.room_info": "requires bound join-link cookie (or session)",
	"grid_interview.api.video.join": "requires bound join-link cookie + custom header (or session + CSRF)",
	"grid_interview.api.video.heartbeat": "same as join",
	"grid_interview.api.video.leave": "same as join",
	"grid_interview.api.video.redeem_link": "single-use hashed token, custom header, rate limited",
	"grid_interview.api.recording.consent_status": "requires bound join-link cookie (or session)",
	"grid_interview.api.recording.set_consent": "requires bound join-link cookie + custom header (or session + CSRF)",
	"grid_interview.api.recording.ingest_start": "HMAC signature with timestamp and replay protection",
	"grid_interview.api.recording.ingest_chunk": "HMAC signature with timestamp and replay protection",
	"grid_interview.api.recording.ingest_complete": "HMAC signature with timestamp and replay protection",
}

READ_PREFIXES = ("get_", "list_", "search_", "room_info", "consent_status", "stream", "download_document")


def _endpoints():
	for mod in pkgutil.iter_modules(api_pkg.__path__):
		importlib.import_module(f"grid_interview.api.{mod.name}")
	return {f"{fn.__module__}.{fn.__name__}": fn for fn in frappe.whitelisted
			if getattr(fn, "__module__", "").startswith("grid_interview.")}


class TestEndpointPolicy(RollbackTestCase):
	def test_guest_endpoints_are_exactly_the_reviewed_set(self):
		endpoints = _endpoints()
		guest = {name for name, fn in endpoints.items() if fn in frappe.guest_methods}
		self.assertEqual(guest, set(GUEST_ALLOWED), "guest endpoints changed: review and update GUEST_ALLOWED")

	def test_state_changing_endpoints_are_post_only(self):
		for name, fn in _endpoints().items():
			methods = frappe.allowed_http_methods_for_whitelisted_func.get(fn) or []
			short = name.rsplit(".", 1)[-1]
			if short.startswith(READ_PREFIXES):
				self.assertNotIn("POST", methods, f"{name} reads data and should be GET-only")
			else:
				self.assertEqual(methods, ["POST"], f"{name} changes state and must be POST-only")

	def test_no_endpoint_is_xss_safe_flagged(self):
		self.assertFalse([n for n, fn in _endpoints().items() if fn in frappe.xss_safe_methods])

	def test_api_modules_do_not_use_ignore_permissions_directly(self):
		# authorization lives in services; API modules must stay thin wrappers
		for mod in pkgutil.iter_modules(api_pkg.__path__):
			source = inspect.getsource(importlib.import_module(f"grid_interview.api.{mod.name}"))
			self.assertNotIn("ignore_permissions", source, mod.name)
			self.assertNotIn("frappe.db.sql", source, mod.name)
