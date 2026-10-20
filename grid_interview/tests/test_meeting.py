"""Phase 5: meeting access, join window, attendance / lifecycle, Jitsi tokens, join links."""

import json
from datetime import timedelta

import jwt

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from grid_interview.exceptions import AccessDenied, InvalidAccessToken, OutsideJoinWindow
from grid_interview.services import candidates, meeting, notifications, portal, scheduling
from grid_interview.tests.utils import INTERVIEW_TYPE, RollbackTestCase, make_applicant, make_interview_type, make_user, needs_hrms
from grid_interview.video import get_provider

SESSION = "Grid Interview Video Session"
ACCESS = "Grid Interview Access Session"
SECRET = "unit-test-jitsi-secret-0123456789abcdef"


def set_settings(**values):
	for k, v in values.items():
		frappe.db.set_single_value("Grid Interview Settings", k, v)
	frappe.clear_cache(doctype="Grid Interview Settings")


@needs_hrms
class MeetingBase(RollbackTestCase):
	def setUp(self):
		super().setUp()
		make_interview_type()
		self.interviewer = make_user("gi.m.int@example.com", ["Interviewer"])
		self.applicant = make_applicant("meet.cand@example.com", "Meet Candidate")
		out = scheduling.schedule_interview(self.applicant, INTERVIEW_TYPE, add_days(nowdate(), 3), "10:00:00",
											"11:00:00", [self.interviewer])
		self.session = out["video_session"]
		self.candidate = "meet.cand@example.com"
		self.place(minutes_from_now=5)  # starts in 5 min, window (15 before) is open

	def place(self, minutes_from_now: int, duration: int = 60):
		start = add_to_date(now_datetime(), minutes=minutes_from_now)
		frappe.db.set_value(SESSION, self.session, {"scheduled_start": start,
													"scheduled_end": start + timedelta(minutes=duration)})

	def as_user(self, user, fn, *args, **kw):
		frappe.set_user(user)
		try:
			return fn(*args, **kw)
		finally:
			frappe.set_user("Administrator")

	def doc(self):
		return frappe.get_doc(SESSION, self.session)

	def clear_presence(self):
		for e in self.doc().participant_events:
			frappe.cache.delete_value(meeting._presence_key(self.session, e.user or e.details))


class TestJoinWindowAndAccess(MeetingBase):
	def test_window_enforced(self):
		self.place(minutes_from_now=60)  # opens in 45 min
		with self.assertRaises(OutsideJoinWindow):
			self.as_user(self.candidate, meeting.join, self.session)
		self.place(minutes_from_now=-200)  # ended 140 min ago, window closed 110 min ago
		with self.assertRaises(OutsideJoinWindow):
			self.as_user(self.candidate, meeting.join, self.session)
		self.place(minutes_from_now=5)
		self.assertEqual(self.as_user(self.candidate, meeting.join, self.session)["participant_kind"], "Candidate")

	def test_cancelled_cannot_be_joined(self):
		scheduling.cancel(self.session)
		with self.assertRaises(OutsideJoinWindow):
			self.as_user(self.interviewer, meeting.join, self.session)

	def test_candidate_config_has_no_secrets_or_moderation(self):
		cfg = self.as_user(self.candidate, meeting.join, self.session)
		self.assertEqual(cfg["provider"], "Mock")
		self.assertFalse(cfg["moderator"])
		self.assertFalse(cfg["recording_allowed"])
		self.assertEqual(cfg["room"], self.doc().room_name)
		for key in cfg:
			self.assertNotIn("secret", key.lower())

	def test_who_may_join(self):
		other_app = make_applicant("other.meet@example.com", "Other")
		scheduling.schedule_interview(other_app, INTERVIEW_TYPE, add_days(nowdate(), 4), "10:00:00", "11:00:00",
									  [self.interviewer])
		with self.assertRaises(frappe.PermissionError):
			self.as_user("other.meet@example.com", meeting.join, self.session)
		stranger = make_user("gi.m.stranger@example.com", ["Interviewer"])
		with self.assertRaises(AccessDenied):
			self.as_user(stranger, meeting.join, self.session)
		with self.assertRaises(AccessDenied):
			self.as_user("Guest", meeting.join, self.session)
		hr = make_user("gi.m.hr@example.com", ["Grid Interview HR User"])
		cfg = self.as_user(hr, meeting.join, self.session)
		self.assertEqual((cfg["participant_kind"], cfg["moderator"]), ("HR", True))

	def test_room_details_disclosure(self):
		cand = self.as_user(self.candidate, meeting.room_details, self.session)
		for key in ("candidate_name", "expected_skills", "job_applicant", "interviewers"):
			self.assertNotIn(key, cand)
		mod = self.as_user(self.interviewer, meeting.room_details, self.session)
		self.assertEqual(mod["candidate_name"], "Meet Candidate")
		self.assertEqual([s["skill"] for s in mod["expected_skills"]], ["GI Test Python"])

	def test_dashboard_join_button_only_in_window(self):
		frappe.set_user(self.candidate)
		card = portal.get_dashboard(candidates.require_candidate())["upcoming"][0]
		self.assertTrue(card["can_join"])
		frappe.set_user("Administrator")
		self.place(minutes_from_now=120)
		frappe.set_user(self.candidate)
		self.assertFalse(portal.get_dashboard(candidates.require_candidate())["upcoming"][0]["can_join"])

	def test_unknown_provider(self):
		with self.assertRaises(ValueError):
			get_provider("Zoom")


class TestLifecycle(MeetingBase):
	def test_candidate_alone_does_not_start_or_complete(self):
		self.as_user(self.candidate, meeting.join, self.session)
		s = self.doc()
		self.assertEqual(s.status, "Scheduled")
		self.assertTrue(s.candidate_joined)
		self.assertEqual([e.event_type for e in s.participant_events], ["Joined"])

	def test_both_join_then_end_completes(self):
		self.as_user(self.candidate, meeting.join, self.session)
		self.as_user(self.interviewer, meeting.join, self.session)
		s = self.doc()
		self.assertEqual(s.status, "In Progress")
		self.assertTrue(s.actual_start)
		self.as_user(self.candidate, meeting.join, self.session)  # reconnect
		self.assertEqual(self.doc().participant_events[-1].event_type, "Reconnected")
		with self.assertRaises(AccessDenied):
			self.as_user(self.candidate, meeting.end_meeting, self.session)
		self.assertEqual(self.as_user(self.interviewer, meeting.end_meeting, self.session)["status"], "Completed")
		self.assertTrue(self.doc().actual_end)
		with self.assertRaises(OutsideJoinWindow):
			self.as_user(self.candidate, meeting.join, self.session)

	def test_end_without_candidate(self):
		self.as_user(self.interviewer, meeting.join, self.session)
		with self.assertRaises(frappe.ValidationError):  # before the start time: wait or cancel
			self.as_user(self.interviewer, meeting.end_meeting, self.session)
		self.place(minutes_from_now=-10)
		self.assertEqual(self.as_user(self.interviewer, meeting.end_meeting, self.session)["status"], "No Show")

	def test_heartbeat_and_leave(self):
		self.as_user(self.candidate, meeting.join, self.session)
		self.assertTrue(meeting.is_present(self.session, self.candidate))
		self.assertTrue(self.as_user(self.candidate, meeting.heartbeat, self.session)["active"])
		self.as_user(self.candidate, meeting.leave, self.session)
		self.assertFalse(meeting.is_present(self.session, self.candidate))
		self.assertEqual(self.doc().participant_events[-1].event_type, "Left")

	def test_finalize_completes_quiet_meeting_after_window(self):
		self.as_user(self.candidate, meeting.join, self.session)
		self.as_user(self.interviewer, meeting.join, self.session)
		self.clear_presence()
		meeting.finalize_sessions()  # still inside the window: only disconnects are recorded
		s = self.doc()
		self.assertEqual(s.status, "In Progress")
		self.assertEqual(sum(e.event_type == "Disconnected" for e in s.participant_events), 2)
		meeting.finalize_sessions()  # idempotent
		self.assertEqual(sum(e.event_type == "Disconnected" for e in self.doc().participant_events), 2)
		self.place(minutes_from_now=-200)
		meeting.finalize_sessions()
		self.assertEqual(self.doc().status, "Completed")

	def test_finalize_no_show_and_expired(self):
		self.as_user(self.interviewer, meeting.join, self.session)
		self.place(minutes_from_now=-200)
		meeting.finalize_sessions()
		self.assertEqual(self.doc().status, "No Show")

	def test_finalize_expired_when_nobody_or_only_candidate(self):
		self.as_user(self.candidate, meeting.join, self.session)
		self.place(minutes_from_now=-200)
		meeting.finalize_sessions()
		self.assertEqual(self.doc().status, "Expired")


class TestJitsiTokens(MeetingBase):
	def setUp(self):
		super().setUp()
		s = frappe.get_single("Grid Interview Settings")
		s.update({"video_provider": "Jitsi", "jitsi_domain": "meet.example.com", "jitsi_app_id": "grid-interview",
				  "jitsi_app_secret": SECRET, "jitsi_jwt_audience": "jitsi", "jitsi_token_ttl_minutes": 120})
		s.save()
		frappe.db.set_value(SESSION, self.session, "provider", "Jitsi")

	def decode(self, cfg):
		return jwt.decode(cfg["jwt"], SECRET, algorithms=["HS256"], audience="jitsi")

	def test_room_scoped_short_lived_tokens(self):
		cfg = self.as_user(self.interviewer, meeting.join, self.session)
		self.assertEqual(cfg["domain"], "meet.example.com")
		self.assertNotIn(SECRET, json.dumps(cfg))
		claims = self.decode(cfg)
		self.assertEqual(claims["room"], self.doc().room_name)
		self.assertEqual((claims["iss"], claims["sub"]), ("grid-interview", "meet.example.com"))
		self.assertTrue(claims["context"]["user"]["moderator"])
		self.assertFalse(claims["context"]["features"]["recording"])
		_opens, closes = meeting.join_window(self.doc())
		seconds_left = (closes - now_datetime()).total_seconds()
		self.assertLessEqual(claims["exp"] - claims["iat"], seconds_left + 5)  # never outlives the window

		cand = self.decode(self.as_user(self.candidate, meeting.join, self.session))
		self.assertFalse(cand["context"]["user"]["moderator"])
		self.assertEqual(cand["context"]["user"]["id"], self.candidate)

	def test_token_signed_with_server_secret_only(self):
		cfg = self.as_user(self.candidate, meeting.join, self.session)
		with self.assertRaises(jwt.InvalidSignatureError):
			jwt.decode(cfg["jwt"], "wrong-secret", algorithms=["HS256"], audience="jitsi")


class TestJoinLinks(MeetingBase):
	def test_disabled_by_default(self):
		with self.assertRaises(frappe.ValidationError):
			meeting.create_link_token(self.session)
		with self.assertRaises(InvalidAccessToken):
			meeting.redeem_link("anything")

	def test_bind_reconnect_and_replay(self):
		set_settings(allow_link_join=1)
		raw = meeting.create_link_token(self.session)
		self.assertFalse(frappe.db.exists(ACCESS, {"token_hash": raw}))  # only the hash is stored
		access, secret = meeting.redeem_link(raw)
		self.assertTrue(secret)
		cookie = f"{access.name}.{secret}"
		again, new_secret = meeting.redeem_link(raw, cookie)  # same browser: reconnect
		self.assertIsNone(new_secret)
		self.assertEqual(again.reconnect_count, 1)
		with self.assertRaises(InvalidAccessToken):
			meeting.redeem_link(raw, None)  # forwarded link, other browser
		with self.assertRaises(InvalidAccessToken):
			meeting.redeem_link(raw, f"{access.name}.wrong")
		self.assertEqual(frappe.db.get_value(ACCESS, access.name, "failed_attempts"), 2)

	def test_too_many_failures_revoke(self):
		set_settings(allow_link_join=1)
		raw = meeting.create_link_token(self.session)
		access, _secret = meeting.redeem_link(raw)
		for _i in range(meeting.MAX_LINK_FAILURES):
			with self.assertRaises(InvalidAccessToken):
				meeting.redeem_link(raw, "x.y")
		self.assertTrue(frappe.db.get_value(ACCESS, access.name, "revoked"))

	def test_link_participant_joins_as_candidate(self):
		set_settings(allow_link_join=1)
		raw = meeting.create_link_token(self.session)
		access, _secret = meeting.redeem_link(raw)
		row = frappe.db.get_value(ACCESS, access.name, ["name", "video_session", "candidate_email", "access_count"], as_dict=True)
		frappe.set_user("Guest")
		cfg = meeting.join(self.session, row)
		self.assertEqual((cfg["participant_kind"], cfg["moderator"]), ("Candidate", False))
		frappe.set_user("Administrator")
		self.assertTrue(self.doc().candidate_joined)
		row.video_session = "some-other-session"
		frappe.set_user("Guest")
		with self.assertRaises(AccessDenied):
			meeting.resolve_participant(self.doc(), row)

	def test_reschedule_and_expiry_invalidate_links(self):
		set_settings(allow_link_join=1)
		raw = meeting.create_link_token(self.session)
		scheduling.revoke_access(self.session, "Interview rescheduled")
		with self.assertRaises(InvalidAccessToken):
			meeting.redeem_link(raw)
		raw2 = meeting.create_link_token(self.session)
		frappe.db.set_value(ACCESS, {"token_hash": ["is", "set"], "revoked": 0}, "expires_on", add_to_date(None, minutes=-1))
		with self.assertRaises(InvalidAccessToken):
			meeting.redeem_link(raw2)

	def test_new_link_replaces_old(self):
		set_settings(allow_link_join=1)
		first = meeting.create_link_token(self.session)
		meeting.create_link_token(self.session)
		with self.assertRaises(InvalidAccessToken):
			meeting.redeem_link(first)

	def test_issue_join_link_email(self):
		set_settings(allow_link_join=1)
		meeting.issue_join_link(self.session)
		log = frappe.get_last_doc("Grid Interview Notification Log", filters={"notification_type": notifications.ACCESS_LINK})
		self.assertEqual(log.recipient, self.candidate)
		self.assertNotIn("join?t=", log.context_json or "")
		self.assertTrue(frappe.db.exists(ACCESS, {"video_session": self.session, "revoked": 0}))
		with self.assertRaises(frappe.PermissionError):
			self.as_user(self.interviewer, meeting.issue_join_link, self.session)
