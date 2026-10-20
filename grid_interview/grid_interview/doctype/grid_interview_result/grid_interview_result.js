// Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grid Interview Result", {
	refresh(frm) {
		frm.set_intro(
			frm.doc.published
				? __("Published: the candidate can see the status, summary, next steps and requested documents.")
				: __("Not visible to the candidate until you publish it. Internal notes are never shown."),
			frm.doc.published ? "green" : "orange",
		);
		if (frm.is_new() || frm.is_dirty()) return;
		const call = (method, msg) =>
			frappe
				.call({ method: "grid_interview.api.hr." + method, args: { name: frm.doc.name }, freeze: true })
				.then(() => {
					frappe.show_alert({ message: msg, indicator: "green" });
					frm.reload_doc();
				});
		if (!frm.doc.published) {
			frm.add_custom_button(__("Publish to Candidate"), () =>
				frappe.confirm(
					__("Publish this result? The candidate will get an email asking them to sign in, and will see it in the portal."),
					() => call("publish_result", __("Published")),
				),
			).addClass("btn-primary");
		} else {
			frm.add_custom_button(__("Unpublish"), () => call("unpublish_result", __("Unpublished")));
		}
	},
});
