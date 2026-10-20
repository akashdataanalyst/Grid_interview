# Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GridInterviewReference(Document):
	def validate(self):
		self.applicant_doctype = self.applicant_doctype or "Job Applicant"
		if self.email:
			self.email = self.email.strip().lower()
		if self.verification_status in ("Verified", "Unable to Verify", "Declined"):
			self.locked = 1
			self.verification_token_hash = None
