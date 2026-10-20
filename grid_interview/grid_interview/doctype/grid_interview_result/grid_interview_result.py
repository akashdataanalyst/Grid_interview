# Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

CANDIDATE_TEXT_FIELDS = ("candidate_status", "result_summary", "next_steps", "documents_requested")


class GridInterviewResult(Document):
	def validate(self):
		self.applicant_doctype = self.applicant_doctype or "Job Applicant"
		self.interview_doctype = self.interview_doctype or "Interview"
		if not self.job_applicant:
			frappe.throw(_("Job Applicant is required."))
		if not self.is_new() and not self.flags.grid_interview_publish:
			if self.has_value_changed("published"):
				frappe.throw(_("Use the Publish / Unpublish buttons."))
			before = self.get_doc_before_save()
			if before and before.published and any(self.has_value_changed(f) for f in CANDIDATE_TEXT_FIELDS):
				frappe.throw(_("Unpublish the result before changing what the candidate sees."))
		elif self.is_new() and self.published and not self.flags.grid_interview_publish:
			frappe.throw(_("Save the result first, then publish it."))
