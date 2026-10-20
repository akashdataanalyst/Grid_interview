# Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
# For license information, please see license.txt

import shutil

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import strip_html

from grid_interview.utils import audit
from grid_interview.utils.settings import reminder_offsets

# (fieldname, minimum, maximum)
_INT_RANGES = (
	("join_window_before_minutes", 0, 240),
	("join_window_after_end_minutes", 0, 480),
	("access_link_validity_hours", 0, 24 * 60),
	("max_email_attempts", 1, 10),
	("jitsi_token_ttl_minutes", 5, 24 * 60),
	("max_recording_minutes", 1, 600),
	("max_recording_size_mb", 1, 20480),
	("max_concurrent_recordings", 1, 50),
	("signed_url_ttl_seconds", 30, 3600),
	("request_timeout_seconds", 10, 3600),
	("max_job_attempts", 1, 20),
	("retry_base_seconds", 5, 3600),
	("max_upload_size_mb", 1, 50),  # nginx client_max_body_size is 50m in the current stack
	("max_documents_per_candidate", 1, 100),
	("max_references", 1, 20),
	("reference_link_validity_days", 1, 30),
	("presence_timeout_seconds", 30, 1800),
	("min_free_disk_gb", 0, 100000),
	("ingest_chunk_mb", 1, 45),  # below nginx client_max_body_size (50m)
	("ai_max_transcript_chars", 5000, 1000000),
)

EXTERNAL = "OpenAI Compatible"
MIN_SECRET_LENGTH = 32


class GridInterviewSettings(Document):
	def validate(self):
		for fieldname, low, high in _INT_RANGES:
			value = self.get(fieldname)
			if value is not None and not (low <= int(value) <= high):
				frappe.throw(_("{0} must be between {1} and {2}").format(self.meta.get_label(fieldname), low, high))
		try:
			reminder_offsets(self.reminder_offsets_minutes)
		except frappe.ValidationError as e:
			frappe.throw(str(e))
		self.allowed_upload_extensions = ",".join(
			sorted({e.strip().lower().lstrip(".") for e in (self.allowed_upload_extensions or "").split(",") if e.strip()})
		)
		if self.malware_scan_method:
			try:
				if not callable(frappe.get_attr(self.malware_scan_method.strip())):
					raise TypeError
			except Exception:
				frappe.throw(_("Malware Scan Method {0} is not an importable function.").format(self.malware_scan_method))
		self._validate_video()
		self._validate_recording()
		self._validate_processing()

	def _validate_video(self):
		if self.video_provider != "Jitsi":
			return
		self.jitsi_domain = (self.jitsi_domain or "").strip().removeprefix("https://").rstrip("/")
		if not self.jitsi_domain or "/" in self.jitsi_domain:
			frappe.throw(_("Enter the Jitsi host name only, e.g. meet.example.com"))
		secret = self._secret("jitsi_app_secret")
		if not self.jitsi_app_id or not secret:
			frappe.throw(_("Jitsi JWT App ID and App Secret are required so that only authorised users can join rooms."))
		if len(secret) < MIN_SECRET_LENGTH:
			frappe.throw(_("The Jitsi JWT App Secret must be at least {0} characters (use openssl rand -hex 32).").format(MIN_SECRET_LENGTH))

	def _secret(self, fieldname: str) -> str:
		"""The newly typed value, or the stored one when the form shows the masked placeholder."""
		value = self.get(fieldname) or ""
		if not value or set(value) == {"*"}:
			value = self.get_password(fieldname, raise_exception=False) or ""
		return value

	def _validate_recording(self):
		if not self.enable_recording:
			return
		if self.video_provider == "Mock":
			frappe.throw(_("Recording needs a real video provider (Jitsi with Jibri)."))
		if not strip_html(self.consent_text or "").strip():
			frappe.throw(_("Recording consent text is required before recording can be enabled."))
		if not (self.consent_policy_version or "").strip():
			frappe.throw(_("Consent policy version is required."))
		if self.storage_backend == "S3" and not (self.s3_bucket and self.s3_region):
			frappe.throw(_("S3 bucket and region are required for S3 storage."))
		if len(self._secret("recording_webhook_secret")) < MIN_SECRET_LENGTH:
			frappe.throw(_("Set a Recording Webhook Secret of at least {0} characters (shared with the Jibri host).").format(MIN_SECRET_LENGTH))
		# finalize (ffprobe) and transcription (ffmpeg) run inside the Frappe containers
		missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
		if missing:
			frappe.throw(_("Install ffmpeg in the Frappe image before enabling recording ({0} not found).").format(", ".join(missing)))

	def _validate_processing(self):
		if self.local_storage_path and not self.local_storage_path.startswith("/"):
			frappe.throw(_("Local Storage Folder must be an absolute path."))
		uses_external = EXTERNAL in (self.transcription_provider, self.ai_provider)
		if uses_external and not self.enable_external_processing:
			frappe.throw(
				_("Enable 'Allow Sending Recordings/Transcripts to External Providers' before selecting a provider.")
			)
		if self.ai_provider == EXTERNAL and not (self.ai_model or "").strip():
			frappe.throw(_("AI Model is required when an AI provider is selected."))

	def on_update(self):
		changed = [
			f.fieldname
			for f in self.meta.fields
			if f.fieldtype not in ("Section Break", "Column Break") and self.has_value_changed(f.fieldname)
		]
		if changed:
			# field names only, never values (some are secrets)
			audit.log("settings.updated", self.doctype, self.name, changed_fields=changed)
