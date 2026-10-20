// Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grid Interview Reference", {
	refresh(frm) {
		if (frm.is_new() || frm.is_dirty()) return;
		const api = "grid_interview.api.hr.";
		if (!["Verified", "Declined", "Unable to Verify"].includes(frm.doc.verification_status)) {
			frm.add_custom_button(
				frm.doc.verification_status === "Verification Sent" ? __("Resend Verification Email") : __("Send Verification Email"),
				() =>
					frappe.confirm(
						__("Email {0} a single-use link to confirm this reference? The reference will be locked for the candidate.", [frm.doc.email]),
						() =>
							frappe
								.call({ method: api + "send_reference_verification", args: { name: frm.doc.name }, freeze: true })
								.then(() => frm.reload_doc()),
					),
			);
			frm.add_custom_button(__("Request Clarification"), () =>
				frappe.prompt(
					{ fieldname: "message", fieldtype: "Small Text", label: __("What should the candidate clarify?"), reqd: 1 },
					(v) =>
						frappe
							.call({ method: api + "request_reference_clarification", args: { name: frm.doc.name, message: v.message } })
							.then(() => frm.reload_doc()),
					__("Request Clarification"),
				),
			);
		}
	},
});
