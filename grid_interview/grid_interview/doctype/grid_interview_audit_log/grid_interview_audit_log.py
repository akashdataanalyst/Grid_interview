# Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GridInterviewAuditLog(Document):
	"""Append-only. Rows are removed only by the retention job."""

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Audit log entries cannot be changed."), frappe.PermissionError)

	def on_trash(self):
		if not frappe.flags.in_grid_interview_retention and not frappe.flags.in_test:
			frappe.throw(_("Audit log entries are removed only by the retention policy."), frappe.PermissionError)
