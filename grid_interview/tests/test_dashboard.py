"""Phase 7: dashboard buckets and scope, final decision, candidate timeline, retention."""

import os
import shutil
import tempfile
import time

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from grid_interview.services import dashboard, retention, results, scheduling
from grid_interview.services.storage import LocalStorage
from grid_interview.tests.utils import (
	INTERVIEW_TYPE,
	RollbackTestCase,
	make_applicant,
	make_interview,
	make_interview_type,
	make_user,
	make_video_session,
	needs_hrms,
)

SESSION = "Grid Interview Video Session"


def set_settings(**values):
	for k, v in values.items():
		frappe.db.set_single_value("Grid Interview Settings", k, v)
	frappe.clear_cache(doctype="Grid Interview Settings")


@needs_hrms
class DashboardBase(RollbackTestCase):
	def setUp(self):
		super().setUp()
		make_interview_type()
		self.int_a = make_user("gi.d.a@example.com", ["Interviewer"])
		self.int_b = make_user("gi.d.b@example.com", ["Interviewer"])
		self.hr = make_user("gi.d.hr@example.com", ["Grid Interview HR User", "HR User"])

	def completed(self, email, interviewer, days=-1):
		interview = make_interview(email, interviewer, days=days)
		session = make_video_session(interview.name)
		frappe.db.set_value(SESSION, session.name, {"status": "Completed", "candidate_joined": 1, "interviewer_joined": 1})
		return interview, session.name

	def upcoming(self, email, interviewer):
		applicant = make_applicant(email)
		return scheduling.schedule_interview(applicant, INTERVIEW_TYPE, add_days(nowdate(), 2), "10:00:00", "11:00:00", [interviewer])

	def give_feedback(self, interview, interviewer):
		fb = frappe.get_doc({"doctype": "Interview Feedback", "interview": interview, "interviewer": interviewer,
							 "result": "Cleared", "skill_assessment": [{"skill": "GI Test Python", "rating": 0.8}]})
		fb.insert(ignore_permissions=True)
		fb.submit()
		return fb

	def names(self, bucket, user="Administrator", **filters):
		frappe.set_user(user)
		try:
			return {r["name"] for r in dashboard.bucket_rows(bucket, filters)}
		finally:
			frappe.set_user("Administrator")


class TestBuckets(DashboardBase):
	def test_classification(self):
		up = self.upcoming("up@example.com", self.int_a)["video_session"]
		_i, done = self.completed("done@example.com", self.int_a)
		self.assertIn(up, self.names("upcoming"))
		self.assertNotIn(done, self.names("upcoming"))
		self.assertIn(done, self.names("completed"))
		self.assertIn(done, self.names("feedback_pending"))
		self.assertIn(done, self.names("awaiting_decision"))
		counts = dashboard.summary({})["counts"]
		self.assertGreaterEqual(counts["completed"], 1)

	def test_feedback_clears_pending(self):
		interview, done = self.completed("fb@example.com", self.int_a)
		self.give_feedback(interview.name, self.int_a)
		self.assertNotIn(done, self.names("feedback_pending"))
		row = next(r for r in dashboard.bucket_rows("completed", {}) if r["name"] == done)
		self.assertEqual((row["feedback_submitted"], row["feedback_expected"]), (1, 1))

	def test_interviewer_sees_only_own(self):
		_i, mine = self.completed("own1@example.com", self.int_a)
		_i, theirs = self.completed("own2@example.com", self.int_b)
		visible = self.names("completed", user=self.int_a)
		self.assertIn(mine, visible)
		self.assertNotIn(theirs, visible)
		self.assertIn(theirs, self.names("completed", user=self.hr))

	def test_others_denied(self):
		cand = make_user("gi.d.cand@example.com", ["Grid Interview Candidate"], user_type="Website User")
		plain = make_user("gi.d.plain@example.com")
		for user in (cand, plain):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				dashboard.summary({})
			frappe.set_user("Administrator")

	def test_filters(self):
		_i, done = self.completed("flt@example.com", self.int_a)
		self.assertIn(done, self.names("completed", interviewer=self.int_a))
		self.assertNotIn(done, self.names("completed", interviewer=self.int_b))
		self.assertNotIn(done, self.names("completed", interview_type="Some Other Round"))
		with self.assertRaises(frappe.ValidationError):
			dashboard.summary({"from_date": "2020-01-01", "to_date": "2026-12-31"})

	def test_filter_values_are_bound_parameters(self):
		# an injection attempt is just a value that matches nothing
		self.assertEqual(self.names("completed", interview_type="x' or '1'='1"), set())


class TestDecision(DashboardBase):
	def test_hr_records_decision_with_feedback_guard(self):
		interview, done = self.completed("dec@example.com", self.int_a)
		frappe.set_user(self.hr)
		with self.assertRaises(frappe.ValidationError):
			dashboard.record_decision(done, "Cleared")  # feedback missing
		out = dashboard.record_decision(done, "Cleared", "Strong answers", override_missing_feedback=1)
		self.assertEqual((out["status"], out["docstatus"]), ("Cleared", 0))
		frappe.set_user("Administrator")
		self.assertNotIn(done, self.names("awaiting_decision"))
		self.assertEqual(frappe.db.get_value("Job Applicant", interview.job_applicant, "status"), "Open")  # unchanged
		self.assertFalse(frappe.db.exists("Grid Interview Result", {"job_applicant": interview.job_applicant}))

	def test_submit_after_feedback(self):
		interview, done = self.completed("dec2@example.com", self.int_a)
		self.give_feedback(interview.name, self.int_a)
		frappe.set_user(self.hr)
		out = dashboard.record_decision(done, "Rejected", submit=1)
		self.assertEqual((out["status"], out["docstatus"]), ("Rejected", 1))
		with self.assertRaises(frappe.ValidationError):
			dashboard.record_decision(done, "Cleared")  # already submitted

	def test_only_hr_and_only_after_interview(self):
		_i, done = self.completed("dec3@example.com", self.int_a)
		frappe.set_user(self.int_a)
		with self.assertRaises(frappe.PermissionError):
			dashboard.record_decision(done, "Cleared", override_missing_feedback=1)
		frappe.set_user("Administrator")
		up = self.upcoming("dec4@example.com", self.int_a)["video_session"]
		with self.assertRaises(frappe.ValidationError):
			dashboard.record_decision(up, "Cleared", override_missing_feedback=1)


class TestTimeline(DashboardBase):
	def test_timeline_contents_and_access(self):
		out = self.upcoming("tl@example.com", self.int_a)
		applicant = frappe.db.get_value("Interview", out["interview"], "job_applicant")
		scheduling.reschedule(out["video_session"], add_days(nowdate(), 2), "12:00:00", "13:00:00")
		r = frappe.get_doc({"doctype": "Grid Interview Result", "job_applicant": applicant, "candidate_status": "Under Review",
							"result_summary": "We will update you soon"}).insert()
		results.publish(r.name)
		titles = [e["title"] for e in dashboard.timeline(applicant)]
		for expected in ("Application received", "Interview scheduled", "Interview rescheduled"):
			self.assertIn(expected, titles)
		self.assertTrue(any(t.startswith("Result published") for t in titles))
		self.assertTrue(frappe.db.exists("Grid Interview Audit Log", {"action": "candidate.timeline_viewed"}))
		frappe.set_user(self.int_a)
		with self.assertRaises(frappe.PermissionError):
			dashboard.timeline(applicant)


@needs_hrms
class TestRetention(RollbackTestCase):
	def setUp(self):
		super().setUp()
		self.store = tempfile.mkdtemp(prefix="gi-ret-")
		set_settings(local_storage_path=self.store)
		make_interview_type()
		interview = make_interview("ret@example.com", make_user("gi.ret.int@example.com", ["Interviewer"]), days=-1)
		self.session = make_video_session(interview.name)

	def tearDown(self):
		super().tearDown()
		shutil.rmtree(self.store, ignore_errors=True)

	def recording(self, status="Stored", retention_days=-1):
		key = f"2026/10/test-{frappe.generate_hash(length=6)}.mp4"
		path = LocalStorage().path(key)
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "wb") as fh:
			fh.write(b"video")
		rec = frappe.get_doc({"doctype": "Grid Interview Recording", "video_session": self.session.name,
							  "interview": self.session.interview, "status": status, "storage_backend": "Local Private",
							  "storage_key": key, "retention_until": add_days(nowdate(), retention_days)}).insert(ignore_permissions=True)
		t = frappe.get_doc({"doctype": "Grid Interview Transcript", "recording": rec.name, "video_session": self.session.name,
							"interview": self.session.interview, "status": "Completed", "full_text": "secret words",
							"segments": [{"start_seconds": 0, "end_seconds": 5, "text": "secret words"}]}).insert(ignore_permissions=True)
		a = frappe.get_doc({"doctype": "Grid Interview Analysis", "interview": self.session.interview, "transcript": t.name,
							"status": "Draft", "summary": "private summary",
							"findings": [{"finding_type": "Strength", "claim": "x"}]}).insert(ignore_permissions=True)
		return rec, path, t, a

	def test_expired_recording_and_derived_data_erased(self):
		rec, path, t, a = self.recording()
		counts = retention.run()
		self.assertEqual(counts["recordings"], 1)
		self.assertFalse(os.path.exists(path))
		rec.reload(), t.reload(), a.reload()
		self.assertEqual((rec.status, rec.storage_key), ("Deleted", None))
		self.assertEqual((t.status, t.full_text, len(t.segments)), ("Deleted", None, 0))
		self.assertEqual((a.status, a.summary, len(a.findings)), ("Deleted", None, 0))
		self.assertTrue(frappe.db.exists("Grid Interview Audit Log", {"action": "retention.run"}))

	def test_unexpired_and_suspended_are_kept(self):
		keep, keep_path, _t, _a = self.recording(retention_days=30)
		hold, hold_path, _t2, _a2 = self.recording(status="Suspended")
		retention.run()
		self.assertTrue(os.path.exists(keep_path))
		self.assertTrue(os.path.exists(hold_path))  # legal hold: never auto-deleted
		self.assertEqual(frappe.db.get_value("Grid Interview Recording", hold.name, "status"), "Suspended")

	def test_old_rows_deleted_by_age(self):
		set_settings(retention_audit_log_days=30, retention_access_session_days=30, retention_notification_log_days=30)
		from grid_interview.utils import audit

		audit.log("test.old")
		old = frappe.db.get_value("Grid Interview Audit Log", {"action": "test.old"}, "name")
		frappe.db.set_value("Grid Interview Audit Log", old, "creation", add_days(now_datetime(), -40), update_modified=False)
		audit.log("test.new")
		retention.run()
		self.assertFalse(frappe.db.exists("Grid Interview Audit Log", old))
		self.assertTrue(frappe.db.exists("Grid Interview Audit Log", {"action": "test.new"}))

	def test_zero_means_keep(self):
		set_settings(retention_audit_log_days=0)
		from grid_interview.utils import audit

		audit.log("test.keep")
		name = frappe.db.get_value("Grid Interview Audit Log", {"action": "test.keep"}, "name")
		frappe.db.set_value("Grid Interview Audit Log", name, "creation", add_days(now_datetime(), -4000), update_modified=False)
		retention.run()
		self.assertTrue(frappe.db.exists("Grid Interview Audit Log", name))

	def test_stale_upload_folders_removed(self):
		root = frappe.get_site_path("private", "grid_interview_ingest", "stale-test")
		os.makedirs(root, exist_ok=True)
		old = time.time() - 72 * 3600
		os.utime(root, (old, old))
		retention.run()
		self.assertFalse(os.path.exists(root))

	def test_recordings_without_upload_are_failed_after_a_day(self):
		# A site may already hold stuck recordings (e.g. staging with the scheduler off); they are
		# failed by the same run, so count them instead of assuming an empty table.
		already_stuck = frappe.db.count("Grid Interview Recording", {
			"status": ["in", ["Recording", "Finalizing", "Uploading", "Requested"]],
			"modified": ["<", add_to_date(now_datetime(), hours=-24)]})
		stuck = frappe.get_doc({"doctype": "Grid Interview Recording", "video_session": self.session.name,
								"interview": self.session.interview, "status": "Finalizing"}).insert(ignore_permissions=True)
		fresh = frappe.get_doc({"doctype": "Grid Interview Recording", "video_session": self.session.name,
								"interview": self.session.interview, "status": "Finalizing"}).insert(ignore_permissions=True)
		frappe.db.set_value("Grid Interview Recording", stuck.name, "modified", add_to_date(now_datetime(), hours=-30),
							update_modified=False)
		self.assertEqual(retention.run()["stuck_recordings"], already_stuck + 1)
		self.assertEqual(frappe.db.get_value("Grid Interview Recording", stuck.name, "status"), "Failed")
		self.assertEqual(frappe.db.get_value("Grid Interview Recording", fresh.name, "status"), "Finalizing")

	def test_manual_deletion_requires_admin_and_reason(self):
		rec, path, _t, _a = self.recording(status="Suspended", retention_days=30)
		hr = make_user("gi.ret.hr@example.com", ["Grid Interview HR User"])
		frappe.set_user(hr)
		with self.assertRaises(frappe.PermissionError):
			retention.admin_delete_recording(rec.name, "erasure request")
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			retention.admin_delete_recording(rec.name, "")
		retention.admin_delete_recording(rec.name, "candidate erasure request")
		self.assertFalse(os.path.exists(path))
		self.assertEqual(frappe.db.get_value("Grid Interview Recording", rec.name, "status"), "Deleted")
