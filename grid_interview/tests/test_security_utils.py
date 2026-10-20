import json

import frappe

from grid_interview.tests.utils import RollbackTestCase
from grid_interview.utils import audit, security
from grid_interview.utils.settings import reminder_offsets


class TestTokens(RollbackTestCase):
	def test_tokens_are_random_and_hashed(self):
		raw1, hash1 = security.new_token()
		raw2, hash2 = security.new_token()
		self.assertNotEqual(raw1, raw2)
		self.assertGreaterEqual(len(raw1), 43)  # 32 bytes urlsafe
		self.assertEqual(len(hash1), 64)
		self.assertNotIn(raw1, hash1)
		self.assertTrue(security.token_matches(raw1, hash1))
		self.assertFalse(security.token_matches(raw2, hash1))
		self.assertFalse(security.token_matches("", hash1))
		self.assertFalse(security.token_matches(raw1, ""))

	def test_room_names_are_unpredictable(self):
		names = {security.new_room_name() for _ in range(50)}
		self.assertEqual(len(names), 50)
		for name in names:
			self.assertRegex(name, r"^gi-[0-9a-f]{32}$")


class TestAudit(RollbackTestCase):
	def test_sanitize_redacts_secrets_and_truncates(self):
		clean = audit.sanitize(
			{"access_token": "abc", "api_key": "k", "Password": "p", "transcript": "t", "note": "x" * 1000, "ok": 1}
		)
		for key in ("access_token", "api_key", "Password", "transcript"):
			self.assertEqual(clean[key], "[redacted]")
		self.assertLessEqual(len(clean["note"]), 301)
		self.assertEqual(clean["ok"], 1)

	def test_log_writes_row_without_secrets(self):
		audit.log("test.action", "User", "Administrator", token="raw-token-value", interview="HR-INT-1")
		row = frappe.get_last_doc("Grid Interview Audit Log", filters={"action": "test.action"})
		self.assertEqual(row.actor, "Administrator")
		self.assertNotIn("raw-token-value", row.metadata)
		self.assertEqual(json.loads(row.metadata)["interview"], "HR-INT-1")

	def test_audit_written_for_missing_reference(self):
		audit.log("test.missing_ref", "Grid Interview Recording", "does-not-exist")
		self.assertTrue(frappe.db.exists("Grid Interview Audit Log", {"action": "test.missing_ref",
																	   "reference_name": "does-not-exist"}))

	def test_audit_rows_are_immutable(self):
		audit.log("test.immutable")
		row = frappe.get_last_doc("Grid Interview Audit Log", filters={"action": "test.immutable"})
		row.action = "tampered"
		with self.assertRaises(frappe.PermissionError):
			row.save(ignore_permissions=True)


class TestSettingsValidation(RollbackTestCase):
	def settings(self):
		return frappe.get_single("Grid Interview Settings")

	def test_reminder_offsets_parse(self):
		self.assertEqual(reminder_offsets("15, 1440,60,60"), [1440, 60, 15])
		self.assertEqual(reminder_offsets(""), [])
		with self.assertRaises(frappe.ValidationError):
			reminder_offsets("10,abc")
		with self.assertRaises(frappe.ValidationError):
			reminder_offsets("0")

	def test_range_validation(self):
		s = self.settings()
		s.join_window_before_minutes = 10000
		with self.assertRaises(frappe.ValidationError):
			s.save()

	def test_jitsi_requires_jwt_credentials(self):
		s = self.settings()
		s.video_provider = "Jitsi"
		s.jitsi_domain = "https://meet.example.com/"
		with self.assertRaises(frappe.ValidationError):
			s.save()
		s = self.settings()  # a failed save leaves a stale in-memory timestamp
		s.video_provider = "Jitsi"
		s.jitsi_domain = "https://meet.example.com/"
		s.jitsi_app_id = "grid"
		s.jitsi_app_secret = "too-short"
		with self.assertRaises(frappe.ValidationError):
			s.save()
		s = self.settings()
		s.video_provider = "Jitsi"
		s.jitsi_domain = "https://meet.example.com/"
		s.jitsi_app_id = "grid"
		s.jitsi_app_secret = "x" * 40
		s.save()
		self.assertEqual(s.jitsi_domain, "meet.example.com")

	def test_recording_needs_strong_webhook_secret(self):
		s = self.settings()
		s.update({"video_provider": "Jitsi", "jitsi_domain": "meet.example.com", "jitsi_app_id": "grid",
				  "jitsi_app_secret": "x" * 40, "enable_recording": 1, "consent_text": "<p>We record.</p>",
				  "recording_webhook_secret": "short"})
		with self.assertRaises(frappe.ValidationError):
			s.save()
		s = self.settings()
		s.update({"video_provider": "Jitsi", "jitsi_domain": "meet.example.com", "jitsi_app_id": "grid",
				  "jitsi_app_secret": "x" * 40, "enable_recording": 1, "consent_text": "<p>We record.</p>",
				  "recording_webhook_secret": "y" * 40})
		s.save()

	def test_recording_requires_real_provider_and_consent_text(self):
		s = self.settings()
		s.enable_recording = 1
		with self.assertRaises(frappe.ValidationError):
			s.save()  # Mock provider

	def test_external_providers_need_explicit_opt_in(self):
		s = self.settings()
		s.transcription_provider = "OpenAI Compatible"
		with self.assertRaises(frappe.ValidationError):
			s.save()
		s = self.settings()
		s.transcription_provider = "OpenAI Compatible"
		s.enable_external_processing = 1
		s.save()

	def test_settings_change_is_audited_without_values(self):
		s = self.settings()
		s.support_email = "help@example.com"
		s.save()
		row = frappe.get_last_doc("Grid Interview Audit Log", filters={"action": "settings.updated"})
		self.assertIn("support_email", row.metadata)
		self.assertNotIn("help@example.com", row.metadata)
