// Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grid Interview Processing Job", {
	refresh(frm) {
		if (["Failed", "Cancelled", "Retry Scheduled"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry Now"), () =>
				frappe
					.call({ method: "grid_interview.api.recording.retry_job", args: { name: frm.doc.name } })
					.then(() => frm.reload_doc()),
			);
		}
	},
});
