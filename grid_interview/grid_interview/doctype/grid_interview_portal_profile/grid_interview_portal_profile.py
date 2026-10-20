# Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GridInterviewPortalProfile(Document):
	def validate(self):
		self.email = (self.email or frappe.db.get_value("User", self.user, "email") or "").strip().lower()
		if self.has_value_changed("status") and not self.is_new():
			from grid_interview.utils import audit

			audit.log("candidate.status_changed", self.doctype, self.name, status=self.status)
			if self.status == "Disabled":
				frappe.db.set_value(
					"Grid Interview Access Session",
					{"candidate_user": self.user, "revoked": 0},
					{"revoked": 1, "revoked_on": frappe.utils.now_datetime(), "revoke_reason": _("Account disabled")},
				)
