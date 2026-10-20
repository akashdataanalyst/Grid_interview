// Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grid Interview Video Session", {
	refresh(frm) {
		if (frm.is_new()) return;
		const api = "grid_interview.api.scheduling.";

		if (["Scheduled", "In Progress"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Open Interview Room"), () =>
				window.open(`/interview/room?session=${encodeURIComponent(frm.doc.name)}`, "_blank", "noopener"),
			).addClass("btn-primary");
		}
		if (frm.doc.status === "Scheduled") {
			frm.add_custom_button(__("Send Join Link"), () =>
				frappe.confirm(
					__("Email the candidate a personal join link that works without signing in? Any earlier link stops working."),
					() =>
						frappe
							.call({ method: "grid_interview.api.video.issue_join_link", args: { video_session: frm.doc.name } })
							.then(() => frappe.show_alert({ message: __("Join link queued"), indicator: "green" })),
				),
			);
		}

		if (["Scheduled", "Expired", "No Show"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Reschedule"), () =>
				grid_interview.reschedule_dialog(frm.doc, () => frm.reload_doc()),
			);
		}
		if (frm.doc.status === "Scheduled") {
			frm.add_custom_button(__("Resend Invitation"), () =>
				frappe
					.call({ method: api + "resend_invitation", args: { video_session: frm.doc.name } })
					.then((r) => frappe.show_alert(__("Queued for {0} recipient(s)", [r.message.recipients]))),
			);
		}
		if (!["Cancelled", "Completed", "In Progress"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Cancel Interview"), () =>
				grid_interview.cancel_dialog(frm.doc, () => frm.reload_doc()),
			);
		}
		frm.add_custom_button(__("Notifications"), () => grid_interview.show_notifications(frm.doc.name));
	},
});
