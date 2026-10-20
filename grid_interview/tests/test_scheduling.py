"""Phase 3: scheduling on standard HRMS Interviews, notifications, reminders, reconcile."""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, get_datetime, nowdate

from grid_interview.services import notifications, scheduling
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

LOG = "Grid Interview Notification Log"
SESSION = "Grid Interview Video Session"
DAY = add_days(nowdate(), 3)


def logs(session, **filters):
	return frappe.get_all(
		LOG,
		filters={"reference_name": session, **filters},
		fields=["notification_type", "audience", "recipient", "status", "attempts", "email_queue", "last_error", "name"],
	)


@needs_hrms
class SchedulingBase(RollbackTestCase):
	def setUp(self):
		super().setUp()
		make_interview_type()
		self.interviewer = make_user("gi.sched.int@example.com", ["Interviewer"])

	def schedule(self, email="sched.cand@example.com", day=DAY, start="10:00:00", end="11:00:00", **kw):
		applicant = kw.pop("applicant", None) or make_applicant(email)
		return scheduling.schedule_interview(
			applicant, kw.pop("interview_type", INTERVIEW_TYPE), day, start, end,
			kw.pop("interviewers", [self.interviewer]), **kw,
		)


class TestScheduleInterview(SchedulingBase):
	def test_creates_standard_interview_session_and_notices(self):
		out = self.schedule(instructions="Bring your portfolio")
		interview = frappe.get_doc("Interview", out["interview"])
		self.assertEqual(interview.docstatus, 0)
		self.assertEqual(interview.status, "Pending")
		self.assertEqual([d.interviewer for d in interview.interview_details], [self.interviewer])

		session = frappe.get_doc(SESSION, out["video_session"])
		self.assertEqual(session.interview, interview.name)
		self.assertEqual(session.status, "Scheduled")
		self.assertEqual(get_datetime(session.scheduled_start), get_datetime(f"{DAY} 10:00:00"))

		sent = logs(session.name, notification_type=notifications.SCHEDULED)
		self.assertEqual({(r.audience, r.recipient) for r in sent},
						 {("Candidate", "sched.cand@example.com"), ("Interviewer", self.interviewer)})
		for r in sent:
			self.assertEqual(r.status, "Queued")
			self.assertTrue(r.email_queue)
			self.assertEqual(frappe.db.get_value("Email Queue", r.email_queue, "reference_name"), session.name)

	def test_rejects_invalid_windows(self):
		with self.assertRaises(frappe.ValidationError):
			self.schedule(start="11:00:00", end="10:00:00")
		with self.assertRaises(frappe.ValidationError):
			self.schedule(email="past@example.com", day=add_days(nowdate(), -1))
		with self.assertRaises(frappe.ValidationError):
			self.schedule(email="long@example.com", start="08:00:00", end="17:00:00")
		with self.assertRaises(frappe.ValidationError):
			self.schedule(email="short@example.com", start="10:00:00", end="10:00:02")

	def test_rejects_interviewer_without_role(self):
		plain = make_user("gi.norole@example.com")
		with self.assertRaises(frappe.ValidationError):
			self.schedule(interviewers=[plain])
		with self.assertRaises(frappe.ValidationError):
			self.schedule(interviewers=[])

	def test_rejects_second_active_interview_same_round(self):
		applicant = make_applicant("dup@example.com")
		self.schedule(applicant=applicant)
		with self.assertRaises(frappe.ValidationError):
			self.schedule(applicant=applicant, start="14:00:00", end="15:00:00")

	def test_same_round_rules_follow_the_interview_state(self):
		applicant = make_applicant("retake@example.com")
		first = self.schedule(applicant=applicant)["interview"]
		# Under Review is still undecided: reschedule instead
		frappe.db.set_value("Interview", first, "status", "Under Review")
		with self.assertRaisesRegex(frappe.ValidationError, "Reschedule it instead"):
			self.schedule(applicant=applicant, start="14:00:00", end="15:00:00")
		# decided but not submitted: HRMS allows another interview of the round (e.g. a retake)
		frappe.db.set_value("Interview", first, "status", "Rejected")
		second = self.schedule(applicant=applicant, day=add_days(DAY, 1))["interview"]
		self.assertNotEqual(second, first)
		# submitted: HRMS allows no other interview of the round; the message says what to do
		frappe.db.set_value("Interview", second, {"status": "Cleared", "docstatus": 1})
		frappe.db.set_value("Interview", first, "status", "Cancelled")
		with self.assertRaisesRegex(frappe.ValidationError, "another Interview Type"):
			self.schedule(applicant=applicant, day=add_days(DAY, 2))

	def test_interviewer_overlap_detected_and_overridable(self):
		self.schedule(email="o1@example.com")
		with self.assertRaises(scheduling.SchedulingConflict):
			self.schedule(email="o2@example.com", start="10:30:00", end="11:30:00")
		out = self.schedule(email="o3@example.com", start="10:30:00", end="11:30:00", allow_overlap=1)
		self.assertTrue(out["interview"])

	def test_non_hr_user_cannot_schedule(self):
		frappe.set_user(self.interviewer)  # Interviewer role only
		with self.assertRaises(frappe.PermissionError):
			self.schedule(email="perm@example.com")

	def test_hrms_permissions_still_apply(self):
		# Grid Interview HR User without any HRMS role may not create HRMS Interviews.
		applicant = make_applicant("perm2@example.com")
		hr = make_user("gi.onlyrole@example.com", ["Grid Interview HR User"])
		frappe.set_user(hr)
		with self.assertRaises(frappe.PermissionError):
			scheduling.schedule_interview(applicant, INTERVIEW_TYPE, DAY, "10:00:00", "11:00:00", [self.interviewer])

	def test_instructions_are_html_escaped_in_email(self):
		out = self.schedule(instructions="Room 5 < 6 & bring ID")
		log = frappe.get_doc(LOG, logs(out["video_session"], audience="Candidate")[0].name)
		_subject, message = notifications.render(log)
		self.assertIn("Room 5 &lt; 6 &amp; bring ID", message)  # escaped once, not twice
		self.assertIn("/interview-portal", message)

	def test_script_never_reaches_email(self):
		# Frappe sanitizes HTML on save and build_context escapes again: defence in depth.
		out = self.schedule(email="xss@example.com", instructions='<script>alert("x")</script><img src=x onerror=alert(1)>')
		log = frappe.get_doc(LOG, logs(out["video_session"], audience="Candidate")[0].name)
		_subject, message = notifications.render(log)
		self.assertNotIn("<script", message)
		self.assertNotIn("<img", message)


class TestRescheduleAndCancel(SchedulingBase):
	def test_reschedule_updates_same_interview(self):
		out = self.schedule()
		res = scheduling.reschedule(out["video_session"], DAY, "15:00:00", "16:00:00", reason="panel busy",
									candidate_message="Sorry for the change")
		self.assertTrue(res["changed"])
		interview = frappe.get_doc("Interview", out["interview"])
		self.assertEqual(str(interview.from_time), "15:00:00")
		self.assertEqual(frappe.db.count("Interview", {"job_applicant": interview.job_applicant}), 1)
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Interview",
													  "reference_name": interview.name, "comment_type": "Info"}))
		session = frappe.get_doc(SESSION, out["video_session"])
		self.assertEqual(get_datetime(session.scheduled_start), get_datetime(f"{DAY} 15:00:00"))
		self.assertEqual(len(logs(session.name, notification_type=notifications.RESCHEDULED)), 2)
		# same time again: no change, no new emails
		self.assertFalse(scheduling.reschedule(session.name, DAY, "15:00:00", "16:00:00")["changed"])
		self.assertEqual(len(logs(session.name, notification_type=notifications.RESCHEDULED)), 2)

	def test_rescheduled_email_mentions_previous_time(self):
		out = self.schedule()
		scheduling.reschedule(out["video_session"], DAY, "15:00:00", "16:00:00")
		row = logs(out["video_session"], notification_type=notifications.RESCHEDULED, audience="Candidate")[0]
		_subject, message = notifications.render(frappe.get_doc(LOG, row.name))
		self.assertIn("10:00", message)
		self.assertIn("15:00", message)

	def test_cancel(self):
		out = self.schedule()
		self.assertTrue(scheduling.cancel(out["video_session"], reason="position filled")["changed"])
		self.assertEqual(frappe.db.get_value("Interview", out["interview"], "status"), "Cancelled")
		self.assertEqual(frappe.db.get_value(SESSION, out["video_session"], "status"), "Cancelled")
		self.assertEqual(len(logs(out["video_session"], notification_type=notifications.CANCELLED)), 2)
		self.assertFalse(scheduling.cancel(out["video_session"])["changed"])
		with self.assertRaises(frappe.ValidationError):
			scheduling.reschedule(out["video_session"], DAY, "15:00:00", "16:00:00")

	def test_resend_is_explicit_but_not_doubled(self):
		out = self.schedule()
		scheduling.resend_invitation(out["video_session"])
		scheduling.resend_invitation(out["video_session"])  # same minute: ignored
		self.assertEqual(len(logs(out["video_session"], notification_type=notifications.SCHEDULED)), 4)


class TestReminders(SchedulingBase):
	def test_reminders_once_per_offset_and_superseded_skipped(self):
		out = self.schedule()
		start = get_datetime(f"{DAY} 10:00:00")
		self.assertEqual(notifications.send_due_reminders(now=start - timedelta(minutes=50)), 2)  # 60-min reminder
		rows = logs(out["video_session"], notification_type=notifications.REMINDER)
		self.assertEqual(sorted(r.status for r in rows), ["Queued", "Queued", "Skipped", "Skipped"])  # 1440 skipped
		self.assertEqual(notifications.send_due_reminders(now=start - timedelta(minutes=45)), 0)  # idempotent
		self.assertEqual(notifications.send_due_reminders(now=start - timedelta(minutes=10)), 2)  # 15-min
		self.assertEqual(notifications.send_due_reminders(now=start + timedelta(minutes=1)), 0)  # started

	def test_no_reminder_right_after_the_invitation(self):
		# scheduled 12 minutes before the start: the 15-minute reminder would duplicate the invitation
		out = self.schedule()
		start = get_datetime(f"{DAY} 10:00:00")
		for row in logs(out["video_session"], notification_type=notifications.SCHEDULED):
			frappe.db.set_value(LOG, row.name, "creation", start - timedelta(minutes=12), update_modified=False)
		self.assertEqual(notifications.send_due_reminders(now=start - timedelta(minutes=10)), 0)
		rows = logs(out["video_session"], notification_type=notifications.REMINDER)
		self.assertEqual({r.status for r in rows}, {"Skipped"})
		self.assertEqual(sum("Not needed" in (r.last_error or "") for r in rows), 2)  # candidate + interviewer

	def test_reschedule_creates_fresh_reminders(self):
		out = self.schedule()
		start = get_datetime(f"{DAY} 10:00:00")
		notifications.send_due_reminders(now=start - timedelta(minutes=50))
		scheduling.reschedule(out["video_session"], DAY, "14:00:00", "15:00:00")
		new_start = get_datetime(f"{DAY} 14:00:00")
		self.assertEqual(notifications.send_due_reminders(now=new_start - timedelta(minutes=50)), 2)

	def test_no_reminders_when_hrms_reminders_enabled(self):
		self.schedule()
		frappe.db.set_single_value("HR Settings", "send_interview_reminder", 1)
		start = get_datetime(f"{DAY} 10:00:00")
		self.assertEqual(notifications.send_due_reminders(now=start - timedelta(minutes=50)), 0)

	def test_no_reminders_for_cancelled(self):
		out = self.schedule()
		scheduling.cancel(out["video_session"])
		start = get_datetime(f"{DAY} 10:00:00")
		self.assertEqual(notifications.send_due_reminders(now=start - timedelta(minutes=50)), 0)


class TestDelivery(SchedulingBase):
	def setUp(self):
		super().setUp()
		# Error Log rows survive rollback; keep expected test failures out of the real error log
		self._log_error = patch("frappe.log_error")
		self._log_error.start()

	def tearDown(self):
		self._log_error.stop()
		super().tearDown()

	def test_failures_retry_with_limit(self):
		with patch("frappe.sendmail", side_effect=RuntimeError("smtp down")):
			out = self.schedule()
			cand = logs(out["video_session"], audience="Candidate")[0]
			self.assertEqual((cand.status, cand.attempts), ("Queued", 1))
			self.assertIn("smtp down", cand.last_error)
			far = add_to_date(None, days=1)
			notifications.sync_delivery_status(now=far)
			notifications.sync_delivery_status(now=far)
		row = frappe.get_doc(LOG, cand.name)
		self.assertEqual((row.status, row.attempts), ("Failed", 3))
		notifications.sync_delivery_status(now=far)  # no more attempts after Failed
		self.assertEqual(frappe.db.get_value(LOG, cand.name, "attempts"), 3)

	def test_backoff_respected(self):
		with patch("frappe.sendmail", side_effect=RuntimeError("smtp down")):
			out = self.schedule()
			cand = logs(out["video_session"], audience="Candidate")[0]
			notifications.sync_delivery_status()  # immediately: backoff not elapsed
		self.assertEqual(frappe.db.get_value(LOG, cand.name, "attempts"), 1)

	def test_email_queue_status_mirrored(self):
		out = self.schedule()
		cand = logs(out["video_session"], audience="Candidate")[0]
		frappe.db.set_value("Email Queue", cand.email_queue, "status", "Sent")
		notifications.sync_delivery_status()
		self.assertEqual(frappe.db.get_value(LOG, cand.name, "status"), "Sent")

	def test_disabled_notifications_are_skipped(self):
		frappe.db.set_single_value("Grid Interview Settings", "enable_email_notifications", 0)
		frappe.clear_cache(doctype="Grid Interview Settings")
		out = self.schedule()
		self.assertEqual({r.status for r in logs(out["video_session"])}, {"Skipped"})

	def test_cycle_never_raises(self):
		with patch.object(scheduling, "reconcile_sessions", side_effect=RuntimeError("boom")):
			scheduling.run_cycle()
		frappe.log_error.assert_called()


class TestReconcile(SchedulingBase):
	def test_hrms_reschedule_detected(self):
		out = self.schedule()
		interview = frappe.get_doc("Interview", out["interview"])
		# what HRMS's own "Reschedule" button does: db_set, no document events
		interview.db_set({"from_time": "16:00:00", "to_time": "17:00:00"})
		scheduling.reconcile_sessions()
		session = frappe.get_doc(SESSION, out["video_session"])
		self.assertEqual(get_datetime(session.scheduled_start), get_datetime(f"{DAY} 16:00:00"))
		self.assertEqual(len(logs(session.name, notification_type=notifications.RESCHEDULED)), 2)
		scheduling.reconcile_sessions()  # idempotent
		self.assertEqual(len(logs(session.name, notification_type=notifications.RESCHEDULED)), 2)

	def test_desk_cancel_detected(self):
		out = self.schedule()
		frappe.db.set_value("Interview", out["interview"], "status", "Cancelled")
		scheduling.reconcile_sessions()
		self.assertEqual(frappe.db.get_value(SESSION, out["video_session"], "status"), "Cancelled")
		self.assertEqual(len(logs(out["video_session"], notification_type=notifications.CANCELLED)), 2)

	def test_new_interviewer_gets_invitation(self):
		out = self.schedule()
		second = make_user("gi.sched.int2@example.com", ["Interviewer"])
		interview = frappe.get_doc("Interview", out["interview"])
		interview.append("interview_details", {"interviewer": second})
		interview.save()
		scheduling.reconcile_sessions()
		recipients = {r.recipient for r in logs(out["video_session"], notification_type=notifications.SCHEDULED)}
		self.assertIn(second, recipients)

	def test_unattended_past_session_expires(self):
		from grid_interview.services import meeting

		interview = make_interview("exp@example.com", self.interviewer, days=-1)
		session = make_video_session(interview.name)
		meeting.finalize_sessions()
		self.assertEqual(frappe.db.get_value(SESSION, session.name, "status"), "Expired")

	def test_attach_desk_interview(self):
		interview = make_interview("attach@example.com", self.interviewer, days=2)
		out = scheduling.attach_existing(interview.name, instructions="Join early")
		self.assertEqual(len(logs(out["video_session"], notification_type=notifications.SCHEDULED)), 2)
		with self.assertRaises(frappe.ValidationError):
			scheduling.attach_existing(interview.name)

	def test_hrms_form_interview_is_auto_managed(self):
		interview = make_interview("auto@example.com", self.interviewer, days=2)
		scheduling.manage_desk_interviews()
		session = frappe.db.get_value(SESSION, {"interview": interview.name})
		self.assertTrue(session)
		self.assertEqual(len(logs(session, notification_type=notifications.SCHEDULED)), 2)
		scheduling.manage_desk_interviews()  # idempotent: no second session, no re-send
		self.assertEqual(frappe.db.count(SESSION, {"interview": interview.name}), 1)
		self.assertEqual(len(logs(session, notification_type=notifications.SCHEDULED)), 2)

	def test_auto_manage_skips_past_cancelled_and_disabled(self):
		past = make_interview("auto.past@example.com", self.interviewer, days=-1)
		cancelled = make_interview("auto.cancel@example.com", self.interviewer, days=2)
		frappe.db.set_value("Interview", cancelled.name, "status", "Cancelled")
		too_short = make_interview("auto.short@example.com", self.interviewer, days=2,
								   from_time="10:00:00", to_time="10:00:30")
		scheduling.manage_desk_interviews()
		self.assertFalse(frappe.db.exists(
			SESSION, {"interview": ["in", [past.name, cancelled.name, too_short.name]]}))
		with self.assertRaises(frappe.ValidationError):
			scheduling.attach_existing(too_short.name)
		frappe.db.set_single_value("Grid Interview Settings", "auto_manage_desk_interviews", 0)
		frappe.clear_cache(doctype="Grid Interview Settings")
		off = make_interview("auto.off@example.com", self.interviewer, days=2)
		scheduling.manage_desk_interviews()
		self.assertFalse(frappe.db.exists(SESSION, {"interview": off.name}))
