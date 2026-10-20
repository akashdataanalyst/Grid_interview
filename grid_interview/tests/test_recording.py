"""Phase 6: consent, recording control, signed chunked ingest, storage, pipeline, AI validation, review."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate
from frappe.utils.password import set_encrypted_password

from grid_interview.services import analysis, consent, jobs, meeting, recording, scheduling, storage, transcription
from grid_interview.tests.utils import INTERVIEW_TYPE, RollbackTestCase, make_applicant, make_interview_type, make_user, needs_hrms

SESSION = "Grid Interview Video Session"
REC = "Grid Interview Recording"
SECRET = "unit-test-webhook-secret"


def set_settings(**values):
	for k, v in values.items():
		frappe.db.set_single_value("Grid Interview Settings", k, v)
	frappe.clear_cache(doctype="Grid Interview Settings")


def make_video(seconds=3) -> str:
	path = os.path.join(tempfile.mkdtemp(prefix="gi-test-video-"), "room_123.mp4")
	subprocess.run(
		["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
		 "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:v", "mpeg4", "-c:a", "aac", path],
		check=True, timeout=60,
	)
	return path


def signed(method, params, secret=SECRET, ts=None):
	ts = str(ts or int(time.time()))
	return ts, recording.sign(secret, ts, method, params)


@needs_hrms
class RecordingBase(RollbackTestCase):
	def setUp(self):
		super().setUp()
		self.storage_dir = tempfile.mkdtemp(prefix="gi-test-store-")
		set_settings(enable_recording=1, local_storage_path=self.storage_dir, transcription_provider="Mock (testing only)",
					 ai_provider="Mock (testing only)", max_concurrent_recordings=1, min_free_disk_gb=0)
		set_encrypted_password("Grid Interview Settings", "Grid Interview Settings", SECRET, "recording_webhook_secret")
		make_interview_type()
		self.interviewer = make_user("gi.r.int@example.com", ["Interviewer"])
		self.reviewer = make_user("gi.r.rev@example.com", ["Grid Interview Reviewer"])
		applicant = make_applicant("rec.cand@example.com", "Rec Candidate")
		out = scheduling.schedule_interview(applicant, INTERVIEW_TYPE, add_days(nowdate(), 3), "10:00:00", "11:00:00",
											[self.interviewer], recording_eligible=1)
		self.session = out["video_session"]
		start = add_to_date(now_datetime(), minutes=-5)
		frappe.db.set_value(SESSION, self.session, {"scheduled_start": start, "scheduled_end": add_to_date(start, minutes=60)})
		self.candidate = "rec.cand@example.com"
		self.cleanup = [self.storage_dir]

	def tearDown(self):
		super().tearDown()
		for path in self.cleanup:
			shutil.rmtree(path, ignore_errors=True)
		shutil.rmtree(frappe.get_site_path("private", "grid_interview_ingest"), ignore_errors=True)

	def as_user(self, user, fn, *args, **kw):
		frappe.set_user(user)
		try:
			return fn(*args, **kw)
		finally:
			frappe.set_user("Administrator")

	def doc(self):
		return frappe.get_doc(SESSION, self.session)

	def both_join(self):
		self.as_user(self.candidate, meeting.join, self.session)
		self.as_user(self.interviewer, meeting.join, self.session)

	def participant(self, user):
		frappe.set_user(user)
		try:
			return meeting.resolve_participant(self.doc())
		finally:
			frappe.set_user("Administrator")

	def consent_all(self, granted=True):
		for user in (self.candidate, self.interviewer):
			consent.record(self.doc(), self.participant(user), consent.RECORDING, granted)

	def start(self):
		self.both_join()
		self.consent_all()
		return self.as_user(self.interviewer, recording.start, self.session)["recording"]

	def upload(self, path, chunk=40_000, room=None, sha=None):
		room = room or self.doc().room_name
		size = os.path.getsize(path)
		digest = sha or hashlib.sha256(open(path, "rb").read()).hexdigest()
		res = recording.ingest_start(room, os.path.basename(path), size, digest)
		if not res["accept"]:
			return res
		count = 0
		with open(path, "rb") as fh:
			while part := fh.read(chunk):
				recording.ingest_chunk(res["recording"], count, hashlib.sha256(part).hexdigest(), part)
				count += 1
		res.update(recording.ingest_complete(res["recording"], count))
		return res


class TestConsent(RecordingBase):
	def test_history_and_policy_version(self):
		self.both_join()
		cand = self.participant(self.candidate)
		consent.record(self.doc(), cand, consent.RECORDING, True)
		self.assertTrue(consent.current(self.session, self.candidate, consent.RECORDING))
		consent.record(self.doc(), cand, consent.RECORDING, False)
		rows = frappe.get_all("Grid Interview Consent", filters={"video_session": self.session}, pluck="status", order_by="creation")
		self.assertEqual(rows, ["Granted", "Withdrawn"])  # history kept, never overwritten
		consent.record(self.doc(), cand, consent.RECORDING, True)
		set_settings(consent_policy_version="v2")
		self.assertFalse(consent.current(self.session, self.candidate, consent.RECORDING))  # old version no longer counts

	def test_blockers_name_missing_participants(self):
		self.assertTrue(consent.recording_blockers(self.doc()))  # candidate not joined
		self.both_join()
		consent.record(self.doc(), self.participant(self.candidate), consent.RECORDING, True)
		blockers = consent.recording_blockers(self.doc())
		self.assertEqual(len(blockers), 1)
		consent.record(self.doc(), self.participant(self.interviewer), consent.RECORDING, True)
		self.assertEqual(consent.recording_blockers(self.doc()), [])


class TestConsentPresence(RecordingBase):
	def test_participant_who_left_does_not_block(self):
		self.both_join()
		hr = make_user("gi.r.hr@example.com", ["Grid Interview HR User"])
		self.as_user(hr, meeting.join, self.session)
		self.as_user(hr, meeting.leave, self.session)
		self.consent_all()
		self.assertEqual(consent.recording_blockers(self.doc()), [])

	def test_unconsented_newcomer_stops_live_recording(self):
		name = self.start()
		hr = make_user("gi.r.hr2@example.com", ["Grid Interview HR User"])
		self.as_user(hr, meeting.join, self.session)
		self.assertEqual(frappe.db.get_value(REC, name, "status"), "Finalizing")
		self.assertFalse(self.as_user(self.interviewer, meeting.heartbeat, self.session)["recording_live"])


class TestRecordingControl(RecordingBase):
	def test_start_requires_everyones_consent(self):
		self.both_join()
		with self.assertRaises(frappe.ValidationError):
			self.as_user(self.interviewer, recording.start, self.session)
		self.consent_all()
		name = self.as_user(self.interviewer, recording.start, self.session)["recording"]
		self.assertEqual(frappe.db.get_value(REC, name, "status"), "Recording")
		self.assertEqual(self.doc().recording_status, "Recording")
		self.assertTrue(self.as_user(self.candidate, meeting.heartbeat, self.session)["recording_live"])

	def test_only_moderator_and_one_at_a_time(self):
		self.both_join()
		self.consent_all()
		with self.assertRaises(frappe.PermissionError):
			self.as_user(self.candidate, recording.start, self.session)
		self.as_user(self.interviewer, recording.start, self.session)
		with self.assertRaises(frappe.ValidationError):
			self.as_user(self.interviewer, recording.start, self.session)

	def test_disabled_or_ineligible(self):
		self.both_join()
		self.consent_all()
		frappe.db.set_value(SESSION, self.session, "recording_eligible", 0)
		self.assertTrue(recording.can_start(self.doc()))
		frappe.db.set_value(SESSION, self.session, "recording_eligible", 1)
		set_settings(enable_recording=0)
		self.assertTrue(recording.can_start(self.doc()))

	def test_withdrawal_suspends_and_blocks_processing(self):
		name = self.start()
		consent.record(self.doc(), self.participant(self.candidate), consent.RECORDING, False)
		self.assertEqual(frappe.db.get_value(REC, name, "status"), "Suspended")
		self.assertEqual(self.doc().recording_status, "Stopped")
		self.assertFalse(self.as_user(self.interviewer, meeting.heartbeat, self.session)["recording_live"])

	def test_withdrawal_with_delete_policy_refuses_upload(self):
		set_settings(on_consent_withdrawal="Delete")
		self.start()
		consent.record(self.doc(), self.participant(self.candidate), consent.RECORDING, False)
		video = make_video()
		self.cleanup.append(os.path.dirname(video))
		self.assertFalse(self.upload(video)["accept"])

	def test_ending_meeting_stops_recording(self):
		name = self.start()
		self.as_user(self.interviewer, meeting.end_meeting, self.session)
		self.assertEqual(frappe.db.get_value(REC, name, "status"), "Finalizing")


class TestIngestSecurity(RecordingBase):
	def test_signature_checks(self):
		params = {"room": "r", "filename": "a.mp4", "size": "10", "sha256": "0" * 64}
		ts, sig = signed("ingest_start", params)
		recording.verify_signature("ingest_start", params, ts, sig)
		with self.assertRaises(frappe.PermissionError):  # replay
			recording.verify_signature("ingest_start", params, ts, sig)
		ts, sig = signed("ingest_start", params, secret="wrong")
		with self.assertRaises(frappe.PermissionError):
			recording.verify_signature("ingest_start", params, ts, sig)
		old = int(time.time()) - 3600
		ts, sig = signed("ingest_start", params, ts=old)
		with self.assertRaises(frappe.PermissionError):
			recording.verify_signature("ingest_start", params, ts, sig)
		ts, sig = signed("ingest_start", params)
		with self.assertRaises(frappe.PermissionError):  # tampered parameter
			recording.verify_signature("ingest_start", {**params, "room": "other"}, ts, sig)

	def test_unauthorised_room_is_discarded(self):
		video = make_video()
		self.cleanup.append(os.path.dirname(video))
		self.assertFalse(self.upload(video)["accept"])  # no recording was started
		self.assertFalse(self.upload(video, room="gi-unknown")["accept"])
		self.assertTrue(frappe.db.exists("Grid Interview Audit Log", {"action": "recording.unauthorized_discarded"}))

	def test_chunk_checks(self):
		self.start()
		video = make_video()
		self.cleanup.append(os.path.dirname(video))
		res = recording.ingest_start(self.doc().room_name, "x.mp4", os.path.getsize(video), "a" * 64)
		with self.assertRaises(frappe.ValidationError):
			recording.ingest_chunk(res["recording"], 0, "0" * 64, b"data")  # checksum mismatch
		with self.assertRaises(frappe.ValidationError):
			recording.ingest_complete(res["recording"], 3)  # chunks missing
		self.assertFalse(recording.ingest_start(self.doc().room_name, "evil.exe", 10, "a" * 64)["accept"])


class TestPipeline(RecordingBase):
	def run_job(self, name):
		jobs.run(name)
		return frappe.get_doc("Grid Interview Processing Job", name)

	def test_full_pipeline_with_mock_providers(self):
		rec_name = self.start()
		self.as_user(self.interviewer, recording.stop, self.session)
		video = make_video(seconds=3)
		self.cleanup.append(os.path.dirname(video))
		res = self.upload(video)
		self.assertTrue(res["accept"])
		self.assertEqual(self.run_job(res["job"]).status, "Succeeded")

		rec = frappe.get_doc(REC, rec_name)
		self.assertEqual(rec.status, "Stored")
		self.assertEqual(rec.sha256, hashlib.sha256(open(video, "rb").read()).hexdigest())
		self.assertIn(rec.duration_seconds, (2, 3))
		path = storage.LocalStorage().path(rec.storage_key)
		self.assertTrue(path.startswith(self.storage_dir))
		self.assertNotIn("/private/files/", path)
		self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o640")
		self.assertTrue(rec.retention_until)

		tjob = frappe.db.get_value("Grid Interview Processing Job", {"idempotency_key": f"Transcription:{rec_name}"}, "name")
		self.assertEqual(self.run_job(tjob).status, "Succeeded")
		t = frappe.get_last_doc("Grid Interview Transcript", filters={"recording": rec_name})
		self.assertEqual(t.status, "Completed")
		self.assertTrue(all(s.speaker_uncertain for s in t.segments))

		ajob = frappe.db.get_value("Grid Interview Processing Job", {"idempotency_key": f"Analysis:{t.name}:v1"}, "name")
		self.assertEqual(self.run_job(ajob).status, "Succeeded")
		a = frappe.get_last_doc("Grid Interview Analysis", filters={"transcript": t.name})
		self.assertEqual((a.status, a.review_status), ("Draft", "Pending Review"))
		self.assertEqual(a.provisional_recommendation, "Insufficient Evidence")
		self.assertTrue(a.findings)
		self.assertEqual(self.doc().processing_status, "Completed")
		self.assertTrue(frappe.db.exists("Grid Interview Notification Log", {"notification_type": "analysis_ready",
																		   "recipient": self.reviewer}))
		# the AI never touches HRMS feedback or candidate results
		self.assertFalse(frappe.db.exists("Interview Feedback", {"interview": self.doc().interview}))
		self.assertFalse(frappe.db.exists("Grid Interview Result", {"interview": self.doc().interview}))

		# idempotent: running finished jobs again changes nothing
		self.assertEqual(self.run_job(res["job"]).attempts, 1)
		self.assertEqual(frappe.db.count("Grid Interview Transcript", {"recording": rec_name}), 1)

	def test_duplicate_upload_answered_idempotently(self):
		self.start()
		video = make_video()
		self.cleanup.append(os.path.dirname(video))
		res = self.upload(video)
		self.run_job(res["job"])
		# a stale authorised recording exists and Jibri re-sends the same file
		self.as_user(self.interviewer, recording.stop, self.session)
		frappe.get_doc({"doctype": REC, "video_session": self.session, "interview": self.doc().interview,
						"status": "Finalizing"}).insert(ignore_permissions=True)
		again = self.upload(video)
		self.assertFalse(again["accept"])
		self.assertIn("already received", again["reason"])

	def test_integrity_failure_marks_failed(self):
		self.start()
		video = make_video()
		self.cleanup.append(os.path.dirname(video))
		with patch("frappe.log_error"):
			res = self.upload(video, sha="b" * 64)  # declared checksum differs from the content
			job = self.run_job(res["job"])
		self.assertEqual(job.status, "Failed")
		self.assertEqual(frappe.db.get_value(REC, res["recording"], "status"), "Failed")
		self.assertEqual(self.doc().processing_status, "Failed")

	def test_transient_failure_retries_with_backoff(self):
		rec_name = self.start()
		video = make_video()
		self.cleanup.append(os.path.dirname(video))
		res = self.upload(video)
		self.run_job(res["job"])
		tjob = frappe.db.get_value("Grid Interview Processing Job", {"idempotency_key": f"Transcription:{rec_name}"}, "name")
		with patch("frappe.log_error"), patch.object(transcription, "_mock_segments", side_effect=RuntimeError("provider down")):
			job = self.run_job(tjob)
			self.assertEqual(job.status, "Retry Scheduled")
			self.assertGreater(job.next_retry_on, now_datetime())
			frappe.db.set_value("Grid Interview Processing Job", tjob, "max_attempts", 2)
			frappe.db.set_value("Grid Interview Processing Job", tjob, "status", "Queued")
			self.assertEqual(self.run_job(tjob).status, "Failed")
		self.assertEqual(self.doc().processing_status, "Failed")

	def test_duplicate_submissions_share_one_job(self):
		name = self.start()
		first = jobs.submit("Transcription", REC, name)
		self.assertEqual(jobs.submit("Transcription", REC, name), first)
		self.assertEqual(frappe.db.count("Grid Interview Processing Job", {"reference_name": name}), 1)

	def test_external_transcription_needs_candidate_consent(self):
		set_settings(transcription_provider="OpenAI Compatible", enable_external_processing=1)
		rec = frappe._dict(name="r1", video_session=self.session)
		self.assertIsNone(transcription.queue_for(rec))
		self.both_join()
		consent.record(self.doc(), self.participant(self.candidate), consent.EXTERNAL, True)
		with patch.object(jobs, "submit", return_value="job") as submit:
			self.assertEqual(transcription.queue_for(rec), "job")
			submit.assert_called_once()

	def test_openai_transcription_parsing(self):
		rec_name = self.start()
		video = make_video()
		self.cleanup.append(os.path.dirname(video))
		self.run_job(self.upload(video)["job"])
		set_settings(transcription_provider="OpenAI Compatible", enable_external_processing=1, transcription_model="whisper-1")
		set_encrypted_password("Grid Interview Settings", "Grid Interview Settings", "sk-test", "transcription_api_key")
		reply = MagicMock(status_code=200)
		reply.json.return_value = {"language": "hindi", "segments": [
			{"start": 0.0, "end": 1.5, "text": " Namaste, apna introduction dijiye.", "avg_logprob": -0.1},
			{"start": 1.5, "end": 2.9, "text": " I worked on SAP for three years.", "avg_logprob": -0.4}]}
		rec = frappe.get_doc(REC, rec_name)
		with patch("grid_interview.services.transcription.requests.post", return_value=reply) as post:
			segments, language, model = transcription._external(rec, frappe.get_cached_doc("Grid Interview Settings"))
		self.assertEqual(language, "hindi")
		self.assertEqual(len(segments), 2)
		self.assertTrue(all(s["speaker_uncertain"] for s in segments))
		self.assertAlmostEqual(segments[0]["confidence"], 0.905, places=2)
		self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk-test")
		reply.status_code = 401
		with patch("grid_interview.services.transcription.requests.post", return_value=reply):
			with self.assertRaises(jobs.PermanentError):
				transcription._external(rec, frappe.get_cached_doc("Grid Interview Settings"))


class TestPlaybackAndStorage(RecordingBase):
	def test_playback_permissions(self):
		rec = frappe._dict(interview=self.doc().interview)
		self.assertTrue(recording.can_view(rec, self.interviewer))
		self.assertTrue(recording.can_view(rec, self.reviewer))
		self.assertFalse(recording.can_view(rec, self.candidate))
		other = make_user("gi.r.other@example.com", ["Interviewer"])
		self.assertFalse(recording.can_view(rec, other))

	def test_suspended_or_unauthorised_stream_refused(self):
		name = self.start()
		frappe.set_user(self.candidate)
		with self.assertRaises(frappe.PermissionError):
			recording.stream(name)
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			recording.stream(name)  # not Stored yet

	def test_local_storage_rejects_path_traversal(self):
		local = storage.LocalStorage()
		for key in ("../../etc/passwd", "/etc/passwd", "a/../../b"):
			with self.assertRaises(ValueError):
				local.path(key)

	def test_s3_backend_encrypts_verifies_and_presigns(self):
		set_settings(storage_backend="S3", s3_bucket="gi-private", s3_region="ap-south-1", s3_prefix="gi/")
		client = MagicMock()
		client.generate_presigned_url.return_value = "https://signed.example/x"
		src = tempfile.NamedTemporaryFile(delete=False)
		src.write(b"12345")
		src.close()
		client.head_object.return_value = {"ContentLength": 5}
		with patch("boto3.client", return_value=client):
			s3 = storage.S3Storage()
			self.assertEqual(s3.put(src.name, "2026/10/r.mp4", "video/mp4"), 5)
			args = client.upload_file.call_args
			self.assertEqual(args.args[1:], ("gi-private", "gi/2026/10/r.mp4"))
			self.assertEqual(args.kwargs["ExtraArgs"]["ServerSideEncryption"], "AES256")
			url = s3.presigned_url("2026/10/r.mp4", "r.mp4", False)
		self.assertEqual(url, "https://signed.example/x")
		self.assertEqual(client.generate_presigned_url.call_args.kwargs["ExpiresIn"], 300)
		self.assertFalse(os.path.exists(src.name))

	def test_s3_size_mismatch_is_an_error(self):
		set_settings(storage_backend="S3", s3_bucket="gi-private", s3_region="ap-south-1")
		client = MagicMock()
		client.head_object.return_value = {"ContentLength": 1}
		src = tempfile.NamedTemporaryFile(delete=False)
		src.write(b"12345")
		src.close()
		self.cleanup.append(src.name)
		with patch("boto3.client", return_value=client), self.assertRaises(OSError):
			storage.S3Storage().put(src.name, "k.mp4", "video/mp4")


SEGMENTS = [
	{"start": 0, "end": 30, "text": "Tell me about your experience with SAP."},
	{"start": 30, "end": 95, "text": "I implemented SAP FICO for two plants and handled the month end closing."},
]
GOOD = {
	"summary": "Discussed SAP FICO experience.",
	"questions_answers": [{"question": "SAP experience?", "answer_summary": "Implemented FICO", "start_seconds": 0}],
	"skills": [{"skill": "SAP FICO", "rating": "Demonstrated", "evidence": "I implemented SAP FICO for two plants",
				"start_seconds": 30, "basis": "Transcript Fact"}],
	"strengths": [], "gaps": [], "clarifications": [], "follow_up_questions": ["Ask about race condition handling in batch jobs."],
	"uncertainty": [], "provisional_recommendation": "Hold",
}


class TestAIValidation(RollbackTestCase):
	def test_valid_report(self):
		report, warnings = analysis.validate(json.dumps(GOOD), SEGMENTS, ["SAP FICO"])
		self.assertEqual(report["skills"][0]["basis"], "Transcript Fact")
		self.assertEqual(warnings, [])  # "race condition" is not a protected trait

	def test_fenced_json_accepted(self):
		analysis.validate("```json\n" + json.dumps(GOOD) + "\n```", SEGMENTS, ["SAP FICO"])

	def test_structural_errors_rejected(self):
		for bad in ("not json", "[]", {**GOOD, "provisional_recommendation": "Hire"},
					{**GOOD, "skills": [{**GOOD["skills"][0], "rating": "Excellent"}]},
					{**GOOD, "summary": ""},
					{**GOOD, "questions_answers": [{"question": "x", "answer_summary": "y", "start_seconds": 9999}]}):
			with self.assertRaises(ValueError, msg=str(bad)[:60]):
				analysis.validate(bad if isinstance(bad, str) else json.dumps(bad), SEGMENTS, ["SAP FICO"])

	def test_protected_traits_rejected(self):
		for phrase in ("The candidate's accent was strong.", "She is married.", "Candidate seems introverted.",
					   "Good body language.", "Umar zyada hai."):
			with self.assertRaises(ValueError, msg=phrase):
				analysis.validate(json.dumps({**GOOD, "summary": phrase}), SEGMENTS, ["SAP FICO"])

	def test_unverifiable_fact_downgraded(self):
		bad = {**GOOD, "skills": [{**GOOD["skills"][0], "evidence": "I led a team of fifty people"}]}
		report, warnings = analysis.validate(json.dumps(bad), SEGMENTS, ["SAP FICO"])
		self.assertEqual(report["skills"][0]["basis"], "Interpretation")
		self.assertTrue(warnings)

	def test_short_transcript_forces_insufficient_evidence(self):
		report, warnings = analysis.validate(json.dumps(GOOD), SEGMENTS[:1], ["SAP FICO"])
		self.assertEqual(report["provisional_recommendation"], "Insufficient Evidence")

	def test_prompt_contains_only_job_data(self):
		info = frappe._dict(job_title="Accountant", designation=None, interview_type="Technical")
		messages = analysis.build_messages(info, ["SAP FICO"], SEGMENTS, 100000)
		self.assertIn("Never mention, infer or assess gender", messages[0]["content"])
		self.assertIn("SAP FICO", messages[1]["content"])
		truncated = analysis.build_messages(info, [], SEGMENTS, 50)
		self.assertIn("TRUNCATED", truncated[1]["content"])


class TestHumanReview(RecordingBase):
	def make_draft(self):
		t = frappe.get_doc({"doctype": "Grid Interview Transcript", "recording": "x", "video_session": self.session,
							"interview": self.doc().interview, "status": "Completed",
							"segments": [{"start_seconds": 0, "end_seconds": 90, "text": "hello"}]})
		t.flags.ignore_links = True
		t.insert(ignore_permissions=True)
		a = frappe.get_doc({"doctype": "Grid Interview Analysis", "interview": self.doc().interview, "transcript": t.name,
							"status": "Draft", "review_status": "Pending Review", "report_version": 1})
		a.insert(ignore_permissions=True)
		return a

	def test_reviewer_decisions(self):
		a = self.make_draft()
		with self.assertRaises(frappe.PermissionError):
			self.as_user(self.interviewer, analysis.review, a.name, "Approved")
		with self.assertRaises(frappe.ValidationError):
			self.as_user(self.reviewer, analysis.review, a.name, "Rejected", "")  # notes required
		self.as_user(self.reviewer, analysis.review, a.name, "Corrected", "Fixed the summary", "Corrected text")
		a.reload()
		self.assertEqual((a.review_status, a.reviewed_by, a.summary), ("Corrected", self.reviewer, "Corrected text"))

	def test_regenerate_new_version_and_job(self):
		a = self.make_draft()
		job = self.as_user(self.reviewer, analysis.regenerate, a.name)
		a.reload()
		self.assertEqual((a.report_version, a.status, a.review_status), (2, "Queued", "Pending Review"))
		self.assertEqual(frappe.db.get_value("Grid Interview Processing Job", job, "idempotency_key"), f"Analysis:{a.transcript}:v2")
