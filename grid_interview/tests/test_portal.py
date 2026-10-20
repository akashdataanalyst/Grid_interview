"""Phase 4: candidate accounts, portal data isolation, documents, references, results."""

import email
import glob
import os

import frappe
from frappe.utils import add_days, nowdate

from grid_interview.exceptions import InvalidAccessToken
from grid_interview.services import candidates, notifications, portal, references, results, scheduling
from grid_interview.setup.install import CANDIDATE_ROLE
from grid_interview.tests.utils import (
	INTERVIEW_TYPE,
	RollbackTestCase,
	make_applicant,
	make_interview_type,
	make_user,
	needs_hrms,
)

def _valid_pdf() -> bytes:
	from io import BytesIO

	from pypdf import PdfWriter

	writer, buf = PdfWriter(), BytesIO()
	writer.add_blank_page(width=72, height=72)
	writer.write(buf)
	return buf.getvalue()


PDF = _valid_pdf()
PNG = b"\x89PNG\r\n\x1a\n0000"
LOG = "Grid Interview Notification Log"


def fake_scanner_dirty(content, filename):
	return False


def fake_scanner_clean(content, filename):
	return True


def set_settings(**values):
	for k, v in values.items():
		frappe.db.set_single_value("Grid Interview Settings", k, v)
	frappe.clear_cache(doctype="Grid Interview Settings")


class PortalBase(RollbackTestCase):
	def setUp(self):
		super().setUp()
		self._files_before = set(glob.glob(frappe.get_site_path("private", "files", "*")))

	def tearDown(self):
		super().tearDown()
		# File rows roll back but uploaded bytes stay on disk: remove only files this test created
		for path in set(glob.glob(frappe.get_site_path("private", "files", "*gitest*"))) - self._files_before:
			os.remove(path)

	def candidate(self, email="cand.p@example.com", name="Priya Test"):
		user = candidates.ensure_candidate_account(email, name, "Invitation")
		self.assertTrue(user)
		return user


@needs_hrms
class TestAccounts(PortalBase):
	def test_invitation_creates_website_user_with_role_and_profile(self):
		user = self.candidate("inv@example.com", "Inv Ited")
		doc = frappe.get_doc("User", user)
		self.assertEqual(doc.user_type, "Website User")
		self.assertIn(CANDIDATE_ROLE, frappe.get_roles(user))
		self.assertEqual(frappe.db.get_value("Grid Interview Portal Profile", user, "registration_source"), "Invitation")
		# idempotent
		self.assertEqual(candidates.ensure_candidate_account("INV@example.com", "x", "Invitation"), user)

	def test_existing_system_user_is_never_changed(self):
		staff = make_user("staff.member@example.com", ["Interviewer"])
		self.assertIsNone(candidates.ensure_candidate_account(staff, "Staff", "Invitation"))
		self.assertNotIn(CANDIDATE_ROLE, frappe.get_roles(staff))

	def test_home_page_portal_for_candidates_only(self):
		from grid_interview.permissions import website_user_home_page

		self.assertEqual(website_user_home_page(self.candidate("home@example.com", "Home Page")), "interview-portal")
		# Administrator has every role, including the candidate role: must keep the Desk
		self.assertIsNone(website_user_home_page("Administrator"))
		staff = make_user("home.staff@example.com", ["System Manager", CANDIDATE_ROLE])
		self.assertIsNone(website_user_home_page(staff))

	def test_scheduling_provisions_candidate_account(self):
		make_interview_type()
		interviewer = make_user("gi.p.int@example.com", ["Interviewer"])
		applicant = make_applicant("scheduled.cand@example.com", "Sched Cand")
		scheduling.schedule_interview(applicant, INTERVIEW_TYPE, add_days(nowdate(), 3), "10:00:00", "11:00:00", [interviewer])
		self.assertIn(CANDIDATE_ROLE, frappe.get_roles("scheduled.cand@example.com"))
		row = frappe.get_all(LOG, filters={"recipient": "scheduled.cand@example.com"}, pluck="name")[0]
		_s, message = notifications.render(frappe.get_doc(LOG, row))
		self.assertIn("/interview-portal/password", message)

	def test_registration_disabled_by_default(self):
		with self.assertRaises(frappe.ValidationError):
			candidates.register("New Person", "new.person@example.com", 1)
		self.assertFalse(frappe.db.exists("User", "new.person@example.com"))

	def test_registration_generic_and_no_enumeration(self):
		set_settings(allow_self_registration=1)
		msg_new = candidates.register("New Person", "new.person@example.com", 1)
		user = frappe.get_doc("User", "new.person@example.com")
		self.assertEqual(user.user_type, "Website User")
		self.assertTrue(frappe.db.get_value("Grid Interview Portal Profile", user.name, "privacy_notice_accepted_on"))
		msg_existing = candidates.register("Someone Else", "new.person@example.com", 1)
		self.assertEqual(msg_new, msg_existing)
		self.assertEqual(frappe.db.count("User", {"email": "new.person@example.com"}), 1)
		staff = make_user("reg.staff@example.com", ["Interviewer"])
		self.assertEqual(candidates.register("Staff", staff, 1), msg_new)
		self.assertNotIn(CANDIDATE_ROLE, frappe.get_roles(staff))

	def test_registration_requires_privacy_and_valid_input(self):
		set_settings(allow_self_registration=1)
		with self.assertRaises(frappe.ValidationError):
			candidates.register("New Person", "np2@example.com", 0)
		with self.assertRaises(frappe.ValidationError):
			candidates.register("<script>", "np3@example.com", 1)
		with self.assertRaises(frappe.ValidationError):
			candidates.register("Valid Name", "not-an-email", 1)

	def test_csrf_required_for_candidate_writes(self):
		from types import SimpleNamespace
		from unittest.mock import patch

		user = self.candidate("csrf.cand@example.com")
		frappe.set_user(user)
		frappe.session.data = frappe._dict(csrf_token="expected-token")
		post = SimpleNamespace(method="POST")
		with patch.object(frappe.local, "request", post, create=True):
			with patch("frappe.get_request_header", return_value=None):
				with self.assertRaises(frappe.CSRFTokenError):
					candidates.require_candidate()
			with patch("frappe.get_request_header", return_value="wrong"):
				with self.assertRaises(frappe.CSRFTokenError):
					candidates.require_candidate()
			with patch("frappe.get_request_header", return_value="expected-token"):
				self.assertEqual(candidates.require_candidate().user, user)
			frappe.session.data = frappe._dict()  # no token in session: rejected, not skipped
			with patch("frappe.get_request_header", return_value="anything"):
				with self.assertRaises(frappe.CSRFTokenError):
					candidates.require_candidate()
		with patch.object(frappe.local, "request", SimpleNamespace(method="GET"), create=True):
			candidates.require_candidate()  # reads need no token

	def test_require_candidate(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			candidates.require_candidate()
		frappe.set_user("Administrator")
		staff = make_user("rc.staff@example.com", ["Interviewer"])
		frappe.set_user(staff)
		with self.assertRaises(frappe.PermissionError):
			candidates.require_candidate()
		frappe.set_user("Administrator")
		user = self.candidate("rc.cand@example.com")
		frappe.db.set_value("Grid Interview Portal Profile", user, "status", "Disabled")
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			candidates.require_candidate()


@needs_hrms
class TestDashboardIsolation(PortalBase):
	def setUp(self):
		super().setUp()
		make_interview_type()
		self.interviewer = make_user("gi.d.int@example.com", ["Interviewer"])
		self.app_a = make_applicant("a.cand@example.com", "Cand A")
		self.app_b = make_applicant("b.cand@example.com", "Cand B")
		day = add_days(nowdate(), 4)
		self.sa = scheduling.schedule_interview(self.app_a, INTERVIEW_TYPE, day, "10:00:00", "11:00:00", [self.interviewer],
											   instructions="Bring ID")["video_session"]
		self.sb = scheduling.schedule_interview(self.app_b, INTERVIEW_TYPE, day, "12:00:00", "13:00:00", [self.interviewer])["video_session"]

	def as_candidate(self, email):
		frappe.set_user(email)
		return candidates.require_candidate()

	def test_candidate_sees_only_own_interviews(self):
		profile = self.as_candidate("a.cand@example.com")
		data = portal.get_dashboard(profile)
		self.assertEqual([i["name"] for i in data["upcoming"]], [self.sa])
		self.assertEqual(data["upcoming"][0]["instructions"], "Bring ID")
		self.assertNotIn("interviewers", data["upcoming"][0])  # not disclosed by default
		portal.own_session(profile, self.sa)
		with self.assertRaises(frappe.PermissionError):
			portal.own_session(profile, self.sb)

	def test_interviewer_names_only_when_allowed(self):
		set_settings(disclose_interviewer_names=1)
		data = portal.get_dashboard(self.as_candidate("a.cand@example.com"))
		self.assertIn("interviewers", data["upcoming"][0])

	def test_unpublished_decisions_never_leak(self):
		frappe.db.set_value("Job Applicant", self.app_a, "status", "Rejected")
		scheduling.cancel(self.sa)
		result = frappe.get_doc(
			{"doctype": "Grid Interview Result", "job_applicant": self.app_a, "candidate_status": "Not Selected",
			 "result_summary": "Thank you for your time", "internal_note": "weak SQL"}
		).insert()
		data = portal.get_dashboard(self.as_candidate("a.cand@example.com"))
		self.assertEqual(data["applications"][0]["status"], "Under Review")
		self.assertEqual(data["results"], [])

		frappe.set_user("Administrator")
		results.publish(result.name)
		data = portal.get_dashboard(self.as_candidate("a.cand@example.com"))
		self.assertEqual(data["applications"][0]["status"], "Not Selected")
		self.assertEqual(data["results"][0]["result_summary"], "Thank you for your time")
		self.assertNotIn("internal_note", data["results"][0])

	def test_notification_history_is_own(self):
		history = portal.notification_history(self.as_candidate("a.cand@example.com"))
		self.assertTrue(history)
		frappe.set_user("Administrator")
		recipients = set(frappe.get_all(LOG, filters={"name": ["is", "set"]}, pluck="recipient"))
		self.assertIn("b.cand@example.com", recipients)
		for h in history:
			self.assertEqual(set(h), {"notification_type", "status", "creation", "label"})


@needs_hrms
class TestProfileAndDocuments(PortalBase):
	def setUp(self):
		super().setUp()
		self.user = self.candidate("docs.cand@example.com")
		frappe.set_user(self.user)
		self.profile = candidates.require_candidate()

	def test_profile_update_ignores_protected_fields(self):
		out = portal.update_profile(self.profile, {"phone": "98765", "email": "evil@example.com", "status": "Active",
												  "user": "Administrator", "headline": "Accountant"})
		self.assertEqual(out["phone"], "98765")
		self.assertEqual(out["email"], "docs.cand@example.com")
		with self.assertRaises(frappe.ValidationError):
			portal.update_profile(self.profile, {"headline": "<script>x</script>"})
		with self.assertRaises(frappe.ValidationError):
			portal.update_profile(self.profile, {"linkedin_url": "https://evil.example.com/x"})
		with self.assertRaises(frappe.ValidationError):
			portal.update_profile(self.profile, {"total_experience_years": 99})

	def test_upload_valid_pdf_is_private_and_owned(self):
		out = portal.save_document(self.profile, "../../My CV gitest.pdf", PDF, "Resume")
		f = frappe.get_doc("File", out["name"])
		self.assertTrue(f.is_private)
		self.assertNotIn("/", f.file_name)
		self.assertEqual((f.attached_to_doctype, f.attached_to_name), ("Grid Interview Portal Profile", self.user))
		name, content = portal.get_document_content(self.profile, f.name)
		self.assertEqual(content, PDF)
		self.assertEqual(portal.list_documents(self.profile)[0]["category"], "Resume")

	def test_upload_rejections(self):
		with self.assertRaises(frappe.ValidationError):
			portal.save_document(self.profile, "tool-gitest.exe", b"MZ....", "Document")
		with self.assertRaises(frappe.ValidationError):
			portal.save_document(self.profile, "fake-gitest.pdf", PNG, "Document")  # content mismatch
		with self.assertRaises(frappe.ValidationError):
			portal.save_document(self.profile, "empty-gitest.pdf", b"", "Document")
		with self.assertRaises(frappe.ValidationError):
			portal.save_document(self.profile, "x-gitest.pdf", PDF, "Passport")  # unknown category
		from unittest.mock import patch

		with patch("frappe.log_error"), self.assertRaises(frappe.ValidationError):  # right magic, damaged body
			portal.save_document(self.profile, "broken-gitest.pdf", b"%PDF-1.4\nbroken", "Document")
		set_settings(max_upload_size_mb=1)
		with self.assertRaises(frappe.ValidationError):
			portal.save_document(self.profile, "big-gitest.pdf", PDF + b"0" * (1024 * 1024), "Document")

	def test_malware_hook_fails_closed(self):
		set_settings(malware_scan_method="grid_interview.tests.test_portal.fake_scanner_dirty")
		with self.assertRaises(frappe.ValidationError):
			portal.save_document(self.profile, "cv-gitest.pdf", PDF, "Document")
		set_settings(malware_scan_method="grid_interview.tests.test_portal.fake_scanner_clean")
		self.assertTrue(portal.save_document(self.profile, "cv-gitest.pdf", PDF, "Document"))

	def test_other_candidate_cannot_download_or_delete(self):
		out = portal.save_document(self.profile, "cv-gitest.pdf", PDF, "Document")
		frappe.set_user("Administrator")
		other = self.candidate("other.cand@example.com")
		frappe.set_user(other)
		other_profile = candidates.require_candidate()
		with self.assertRaises(frappe.PermissionError):
			portal.get_document_content(other_profile, out["name"])
		with self.assertRaises(frappe.PermissionError):
			portal.delete_document(other_profile, out["name"])
		frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("Grid Interview Audit Log", {"action": "candidate.document_access_denied"}))


@needs_hrms
class TestReferences(PortalBase):
	def setUp(self):
		super().setUp()
		self.user = self.candidate("ref.cand@example.com", "Ref Cand")
		frappe.set_user(self.user)
		self.profile = candidates.require_candidate()
		self.values = {"reference_person": "Mr Boss", "email": "Boss@Corp.example.com", "relationship": "Manager",
					   "company": "Corp", "consent_confirmed": 1}

	def test_add_requires_consent_and_limits(self):
		with self.assertRaises(frappe.ValidationError):
			references.add_reference(self.profile, {**self.values, "consent_confirmed": 0})
		with self.assertRaises(frappe.ValidationError):
			references.add_reference(self.profile, {**self.values, "email": "ref.cand@example.com"})
		name = references.add_reference(self.profile, self.values)
		self.assertEqual(frappe.db.get_value("Grid Interview Reference", name, "email"), "boss@corp.example.com")
		listed = references.list_references(self.profile)[0]
		self.assertNotIn("internal_notes", listed)
		self.assertNotIn("reference_response", listed)

	def test_verification_flow_single_use(self):
		name = references.add_reference(self.profile, self.values)
		references.update_reference(self.profile, name, {"designation": "CFO"})
		frappe.set_user("Administrator")
		references.send_verification(name)
		doc = frappe.get_doc("Grid Interview Reference", name)
		self.assertEqual((doc.verification_status, doc.locked), ("Verification Sent", 1))
		log = frappe.get_last_doc(LOG, filters={"reference_name": name})
		self.assertEqual(log.recipient, "boss@corp.example.com")
		self.assertTrue(log.email_queue)
		self.assertNotIn("t=", log.context_json or "")  # token never stored in the log
		# Email Queue holds the MIME message (Frappe 16.52+: quoted-printable body). Decode each text
		# part on its own; decoding the whole raw message breaks on random boundaries / base64 parts.
		mime = email.message_from_string(frappe.db.get_value("Email Queue", log.email_queue, "message"))
		message = "".join(
			part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
			for part in mime.walk() if part.get_content_maintype() == "text"
		)
		self.assertIn("/interview-reference?t=", message)

		frappe.set_user(self.user)
		with self.assertRaises(frappe.ValidationError):
			references.update_reference(self.profile, name, {"designation": "CEO"})

		frappe.set_user("Guest")
		raw = references.issue_token(name)
		view = references.referee_view(references.resolve_token(raw))
		self.assertEqual(set(view), {"candidate_name", "job_title", "relationship", "reference_person", "company"})
		references.submit_response(raw, "Yes", period="2021-2024", comments="Reliable", would_recommend="Yes")
		frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.verification_status, "Verified")
		self.assertIn("Reliable", doc.reference_response)
		with self.assertRaises(InvalidAccessToken):
			references.submit_response(raw, "Yes")

	def test_guest_page_lookups_do_not_write_audit_rows(self):
		before = frappe.db.count("Grid Interview Audit Log", {"action": "reference.token_rejected"})
		with self.assertRaises(InvalidAccessToken):
			references.resolve_token("bogus", audit_failure=False)
		self.assertEqual(frappe.db.count("Grid Interview Audit Log", {"action": "reference.token_rejected"}), before)
		with self.assertRaises(InvalidAccessToken):
			references.resolve_token("bogus")  # the rate-limited POST path still audits
		self.assertEqual(frappe.db.count("Grid Interview Audit Log", {"action": "reference.token_rejected"}), before + 1)

	def test_expired_and_unknown_tokens(self):
		name = references.add_reference(self.profile, self.values)
		frappe.set_user("Administrator")
		references.send_verification(name)
		raw = references.issue_token(name)
		frappe.db.set_value("Grid Interview Reference", name, "verification_expires_on", add_days(nowdate(), -1))
		with self.assertRaises(InvalidAccessToken):
			references.resolve_token(raw)
		with self.assertRaises(InvalidAccessToken):
			references.resolve_token("made-up-token")
		with self.assertRaises(InvalidAccessToken):
			references.resolve_token("")

	def test_clarification_unlocks_and_notifies_candidate(self):
		name = references.add_reference(self.profile, self.values)
		frappe.set_user("Administrator")
		references.send_verification(name)
		references.request_clarification(name, "Please give the official email")
		doc = frappe.get_doc("Grid Interview Reference", name)
		self.assertEqual((doc.verification_status, doc.locked, doc.verification_token_hash), ("Clarification Requested", 0, None))
		self.assertTrue(frappe.db.exists(LOG, {"recipient": "ref.cand@example.com", "notification_type": "portal_reference_clarification"}))
		frappe.set_user(self.user)
		references.update_reference(self.profile, name, {"email": "boss@official.example.com"})
		self.assertEqual(frappe.db.get_value("Grid Interview Reference", name, "verification_status"), "Submitted")

	def test_only_hr_manages_verification(self):
		name = references.add_reference(self.profile, self.values)
		with self.assertRaises(frappe.PermissionError):
			references.send_verification(name)  # the candidate themselves
		frappe.set_user("Administrator")
		interviewer = make_user("gi.ref.int@example.com", ["Interviewer"])
		frappe.set_user(interviewer)
		with self.assertRaises(frappe.PermissionError):
			references.send_verification(name)

	def test_other_candidate_cannot_edit(self):
		name = references.add_reference(self.profile, self.values)
		frappe.set_user("Administrator")
		other = self.candidate("ref.other@example.com")
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):
			references.update_reference(candidates.require_candidate(), name, {"company": "X"})


@needs_hrms
class TestResults(PortalBase):
	def make_result(self, **kw):
		applicant = make_applicant("res.cand@example.com", "Res Cand")
		return frappe.get_doc(
			{"doctype": "Grid Interview Result", "job_applicant": applicant, "candidate_status": "Selected",
			 "result_summary": "Congratulations on the offer", **kw}
		).insert()

	def test_publish_sends_generic_email(self):
		r = self.make_result()
		results.publish(r.name)
		r.reload()
		self.assertTrue(r.published)
		log = frappe.get_last_doc(LOG, filters={"reference_name": r.name})
		_subject, message = notifications.render(log)
		self.assertNotIn("Congratulations on the offer", message)
		self.assertIn("/interview-portal", message)

	def test_published_flag_only_via_api(self):
		r = self.make_result()
		r.published = 1
		with self.assertRaises(frappe.ValidationError):
			r.save()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Grid Interview Result", "job_applicant": r.job_applicant,
							"candidate_status": "Selected", "published": 1}).insert()

	def test_published_text_locked_until_unpublished(self):
		r = self.make_result()
		results.publish(r.name)
		r.reload()
		r.result_summary = "changed"
		with self.assertRaises(frappe.ValidationError):
			r.save()
		results.unpublish(r.name)
		r.reload()
		r.result_summary = "changed"
		r.save()

	def test_only_hr_publishes(self):
		r = self.make_result()
		interviewer = make_user("gi.res.int@example.com", ["Interviewer", "HR User"])
		frappe.set_user(interviewer)
		with self.assertRaises(frappe.PermissionError):
			results.publish(r.name)
