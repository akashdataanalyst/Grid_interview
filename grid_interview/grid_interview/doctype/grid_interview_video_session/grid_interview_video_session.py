# Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from grid_interview.utils.security import new_room_name

TERMINAL_STATUSES = ("Completed", "No Show", "Cancelled", "Expired")


class GridInterviewVideoSession(Document):
	def before_insert(self):
		self.room_name = new_room_name()

	def validate(self):
		self.interview_doctype = self.interview_doctype or "Interview"
		self.applicant_doctype = self.applicant_doctype or "Job Applicant"
		if not self.interview:
			frappe.throw(_("Interview is required"))
		duplicate = frappe.db.exists(
			self.doctype,
			{
				"interview_doctype": self.interview_doctype,
				"interview": self.interview,
				"name": ["!=", self.name or ""],
			},
		)
		if duplicate:
			frappe.throw(_("A video session already exists for interview {0}: {1}").format(self.interview, duplicate))
		if self.scheduled_start and self.scheduled_end and self.scheduled_end <= self.scheduled_start:
			frappe.throw(_("Scheduled end must be after scheduled start"))
		if not self.is_new() and self.has_value_changed("room_name"):
			frappe.throw(_("The room name cannot be changed"))

	def add_event(self, event_type: str, participant_type: str, user: str | None = None, details: str | None = None):
		self.append(
			"participant_events",
			{
				"event_type": event_type,
				"participant_type": participant_type,
				"user": user,
				"event_time": frappe.utils.now_datetime(),
				"details": (details or "")[:140],
			},
		)
