from datetime import datetime, timedelta
from unittest.mock import patch

import frappe

from grid_interview.exceptions import HRMSNotAvailable
from grid_interview.integrations import hrms_adapter
from grid_interview.tests.utils import RollbackTestCase, make_interview, make_user, make_video_session, needs_hrms


class TestInterviewWindow(RollbackTestCase):
	def test_regular_window(self):
		start, end = hrms_adapter.interview_window("2026-11-02", "10:00:00", "11:30:00")
		self.assertEqual(start, datetime(2026, 11, 2, 10, 0))
		self.assertEqual(end, datetime(2026, 11, 2, 11, 30))

	def test_timedelta_values_from_db(self):
		start, end = hrms_adapter.interview_window("2026-11-02", timedelta(hours=9, minutes=5), timedelta(hours=9, minutes=50))
		self.assertEqual(start, datetime(2026, 11, 2, 9, 5))
		self.assertEqual(end, datetime(2026, 11, 2, 9, 50))

	def test_past_midnight(self):
		start, end = hrms_adapter.interview_window("2026-11-02", "23:30:00", "00:15:00")
		self.assertEqual(end, datetime(2026, 11, 3, 0, 15))
		self.assertGreater(end, start)


class TestMissingHRMSFallback(RollbackTestCase):
	def test_clear_error_when_hrms_missing(self):
		with patch.object(hrms_adapter, "is_available", return_value=False):
			with self.assertRaises(HRMSNotAvailable):
				hrms_adapter.get_interview("anything")
			self.assertEqual(hrms_adapter.get_applicants_for_email("a@example.com"), [])
			self.assertFalse(hrms_adapter.is_interviewer("a@example.com", "x"))


@needs_hrms
class TestHRMSAdapter(RollbackTestCase):
	def test_get_interview_normalizes_standard_record(self):
		interviewer = make_user("gi.int.a@example.com", ["Interviewer"])
		interview = make_interview("Cand.A@Example.com", interviewer)
		info = hrms_adapter.get_interview(interview.name)
		self.assertEqual(info.job_applicant, interview.job_applicant)
		self.assertEqual(info.applicant_email, "cand.a@example.com")
		self.assertEqual(info.interviewers, (interviewer,))
		self.assertEqual(info.end - info.start, timedelta(hours=1))
		self.assertFalse(info.is_cancelled)

	def test_is_interviewer(self):
		interviewer = make_user("gi.int.b@example.com", ["Interviewer"])
		other = make_user("gi.int.c@example.com", ["Interviewer"])
		interview = make_interview("cand.b@example.com", interviewer)
		self.assertTrue(hrms_adapter.is_interviewer(interviewer, interview.name))
		self.assertFalse(hrms_adapter.is_interviewer(other, interview.name))
		self.assertFalse(hrms_adapter.is_interviewer("Guest", interview.name))

	def test_applicants_by_email_case_insensitive(self):
		interview = make_interview("Mixed.Case@Example.com")
		self.assertIn(interview.job_applicant, hrms_adapter.get_applicants_for_email("mixed.case@example.com"))
		self.assertEqual(hrms_adapter.get_applicants_for_email("nobody@example.com"), [])

	def test_applicants_by_email_has_no_wildcards(self):
		# a candidate signed in as raj.k@ must never see the applicant raj_k@ (or %-matches)
		interview = make_interview("raj_k@example.com")
		self.assertIn(interview.job_applicant, hrms_adapter.get_applicants_for_email("raj_k@example.com"))
		self.assertEqual(hrms_adapter.get_applicants_for_email("raj.k@example.com"), [])
		self.assertEqual(hrms_adapter.get_applicants_for_email("raj%@example.com"), [])

	def test_standard_doctypes_untouched_by_adapter(self):
		interview = make_interview("cand.c@example.com")
		before = frappe.db.get_value("Interview", interview.name, "modified")
		hrms_adapter.get_interview(interview.name)
		self.assertEqual(frappe.db.get_value("Interview", interview.name, "modified"), before)


@needs_hrms
class TestInterviewerRowPermissions(RollbackTestCase):
	def test_interviewer_sees_only_assigned_sessions(self):
		a = make_user("gi.perm.a@example.com", ["Interviewer"])
		b = make_user("gi.perm.b@example.com", ["Interviewer"])
		mine = make_video_session(make_interview("p1@example.com", a).name)
		theirs = make_video_session(make_interview("p2@example.com", b).name)

		frappe.set_user(a)
		visible = set(frappe.get_list("Grid Interview Video Session", pluck="name"))
		self.assertIn(mine.name, visible)
		self.assertNotIn(theirs.name, visible)
		self.assertTrue(frappe.has_permission("Grid Interview Video Session", "read", mine.name))
		self.assertFalse(frappe.has_permission("Grid Interview Video Session", "read", theirs.name))

	def test_hr_user_sees_all(self):
		hr = make_user("gi.hr@example.com", ["Grid Interview HR User"])
		s1 = make_video_session(make_interview("p3@example.com").name)
		frappe.set_user(hr)
		self.assertIn(s1.name, frappe.get_list("Grid Interview Video Session", pluck="name"))

	def test_candidate_cannot_read_app_doctypes(self):
		cand = make_user("gi.cand@example.com", ["Grid Interview Candidate"], user_type="Website User")
		s1 = make_video_session(make_interview("gi.cand@example.com").name)
		frappe.set_user(cand)
		self.assertFalse(frappe.has_permission("Grid Interview Video Session", "read", s1.name))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_list("Grid Interview Video Session")


@needs_hrms
class TestVideoSession(RollbackTestCase):
	def test_room_name_random_and_one_session_per_interview(self):
		interview = make_interview("vs@example.com")
		session = make_video_session(interview.name)
		self.assertRegex(session.room_name, r"^gi-[0-9a-f]{32}$")
		with self.assertRaises(frappe.ValidationError):
			make_video_session(interview.name)

	def test_room_name_immutable(self):
		session = make_video_session(make_interview("vs2@example.com").name)
		session.room_name = "gi-guessable"
		with self.assertRaises(frappe.ValidationError):
			session.save(ignore_permissions=True)
