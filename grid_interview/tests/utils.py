"""Shared test helpers. Every test rolls back its own writes (see RollbackTestCase)."""

import unittest
from datetime import timedelta

import frappe
from frappe.utils import add_days, nowdate

from grid_interview.integrations import hrms_adapter

needs_hrms = unittest.skipUnless(hrms_adapter.is_available(), "needs HRMS")


def reset_settings_to_defaults():
	"""Tests must not depend on whatever a staging site has configured. Runs inside the test
	transaction, so it is rolled back with everything else."""
	for df in frappe.get_meta("Grid Interview Settings").fields:
		if df.fieldtype in ("Section Break", "Column Break", "Tab Break", "Password"):
			continue
		frappe.db.set_single_value("Grid Interview Settings", df.fieldname, df.default if df.default is not None else None)
	frappe.clear_cache(doctype="Grid Interview Settings")


class RollbackTestCase(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		reset_settings_to_defaults()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_cache(doctype="Grid Interview Settings")


def make_user(email: str, roles=(), user_type="System User") -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"user_type": user_type,
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in roles],
			}
		).insert(ignore_permissions=True)
	return email


INTERVIEW_TYPE = "GI Test Technical"


def make_interview_type(name: str = INTERVIEW_TYPE) -> str:
	skill = "GI Test Python"
	if not frappe.db.exists("Skill", skill):
		frappe.get_doc({"doctype": "Skill", "skill_name": skill}).insert(ignore_permissions=True)
	if not frappe.db.exists("Interview Type", name):
		# expected_skill_set is mandatory on Interview Type in hrms 16
		frappe.get_doc(
			{"doctype": "Interview Type", "interview_type_name": name, "expected_skill_set": [{"skill": skill}]}
		).insert(ignore_permissions=True)
	return name


def make_applicant(email: str, name: str = "GI Test Candidate") -> str:
	return frappe.get_doc(
		{"doctype": "Job Applicant", "applicant_name": name, "email_id": email, "status": "Open"}
	).insert(ignore_permissions=True).name


def make_interview(applicant_email="cand@example.com", interviewer=None, days=1, from_time="10:00:00", to_time="11:00:00"):
	"""Create a minimal, valid HRMS Interview (draft) using only standard fields."""
	interviewer = interviewer or make_user("interviewer1@example.com", ["Interviewer"])
	itype_name = make_interview_type()
	applicant = make_applicant(applicant_email)
	interview = frappe.get_doc(
		{
			"doctype": "Interview",
			"interview_type": itype_name,
			"job_applicant": applicant,
			"scheduled_on": add_days(nowdate(), days),
			"from_time": from_time,
			"to_time": to_time,
			"status": "Pending",
			"interview_details": [{"interviewer": interviewer}],
		}
	).insert(ignore_permissions=True)
	return interview


def make_video_session(interview_name: str, **kw):
	info = hrms_adapter.get_interview(interview_name)
	return frappe.get_doc(
		{
			"doctype": "Grid Interview Video Session",
			"interview": interview_name,
			"job_applicant": info.job_applicant,
			"scheduled_start": info.start,
			"scheduled_end": info.end,
			"provider": "Mock",
			**kw,
		}
	).insert(ignore_permissions=True)


ONE_HOUR = timedelta(hours=1)
