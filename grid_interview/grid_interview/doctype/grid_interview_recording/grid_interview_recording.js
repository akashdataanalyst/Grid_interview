// Copyright (c) 2026, Calco PolyTechnik Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("Grid Interview Recording", {
	refresh(frm) {
		if (frm.doc.status === "Suspended") {
			frm.set_intro(__("Suspended after a consent withdrawal: playback and processing are blocked until HR decides."), "orange");
		}
		if (frm.doc.status !== "Stored") return;
		const url = (download) =>
			`/api/method/grid_interview.api.recording.stream?recording_name=${encodeURIComponent(frm.doc.name)}&download=${download}`;
		frm.add_custom_button(__("Play"), () => {
			const d = new frappe.ui.Dialog({ title: __("Recording"), size: "extra-large" });
			d.$body.html(`<video controls preload="metadata" style="width:100%;max-height:70vh;background:#000" src="${url(0)}"></video>
				<p class="text-muted small">${__("Playback is logged in the audit trail.")}</p>`);
			d.onhide = () => d.$body.find("video").each((_, v) => v.pause());
			d.show();
		}).addClass("btn-primary");
		frm.add_custom_button(__("Download"), () => window.open(url(1), "_blank", "noopener"));
	},
});
