// Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grid Interview Analysis", {
	refresh(frm) {
		frm.set_intro(
			__("AI draft for the hiring panel. Check every finding against the transcript and recording. This is not a hiring decision and is never shown to the candidate."),
			"blue",
		);
		const api = "grid_interview.api.recording.";
		const reviewer = frappe.user.has_role(["Grid Interview Reviewer", "Grid Interview Administrator"]) || frappe.session.user === "Administrator";
		if (frm.doc.transcript) {
			frm.add_custom_button(__("Transcript"), () => frappe.set_route("Form", "Grid Interview Transcript", frm.doc.transcript));
		}
		if (!reviewer) return;
		if (frm.doc.status === "Draft") {
			const decide = (decision, needNotes) =>
				frappe.prompt(
					[
						{ fieldname: "notes", fieldtype: "Small Text", label: __("Review notes"), reqd: needNotes ? 1 : 0 },
						...(decision === "Corrected"
							? [{ fieldname: "corrected_summary", fieldtype: "Text", label: __("Corrected summary"), default: frm.doc.summary }]
							: []),
					],
					(v) =>
						frappe
							.call({ method: api + "review_analysis", args: { name: frm.doc.name, decision, ...v } })
							.then(() => frm.reload_doc()),
					__(decision),
				);
			frm.add_custom_button(__("Approve"), () => decide("Approved", false), __("Review"));
			frm.add_custom_button(__("Correct"), () => decide("Corrected", true), __("Review"));
			frm.add_custom_button(__("Reject"), () => decide("Rejected", true), __("Review"));
		}
		if (["Draft", "Failed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Regenerate"), () =>
				frappe.confirm(__("Generate a new version of this report?"), () =>
					frappe.call({ method: api + "regenerate_analysis", args: { name: frm.doc.name } }).then(() => frm.reload_doc()),
				),
			);
		}
	},
});
