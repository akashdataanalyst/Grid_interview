// Shared Desk helpers for Grid Interview (loaded through app_include_js).
// All authorization happens on the server; these only collect input and call whitelisted APIs.

frappe.provide("grid_interview");

grid_interview.API = "grid_interview.api.scheduling.";

grid_interview.split_datetime = (value) => {
	const dt = frappe.datetime.str_to_obj(value);
	return {
		date: frappe.datetime.obj_to_str(dt).slice(0, 10),
		time: moment(dt).format("HH:mm:ss"),
	};
};

grid_interview.reschedule_dialog = (session, on_done) => {
	const start = grid_interview.split_datetime(session.scheduled_start);
	const end = grid_interview.split_datetime(session.scheduled_end);
	const run = (values, allow_overlap) =>
		frappe.call({
			method: grid_interview.API + "reschedule_interview",
			args: { video_session: session.name, ...values, allow_overlap },
			freeze: true,
		});
	const d = new frappe.ui.Dialog({
		title: __("Reschedule Interview"),
		fields: [
			{ fieldname: "scheduled_on", fieldtype: "Date", label: __("Date"), reqd: 1, default: start.date },
			{ fieldtype: "Column Break" },
			{ fieldname: "from_time", fieldtype: "Time", label: __("From"), reqd: 1, default: start.time },
			{ fieldtype: "Column Break" },
			{ fieldname: "to_time", fieldtype: "Time", label: __("To"), reqd: 1, default: end.time },
			{ fieldtype: "Section Break" },
			{ fieldname: "candidate_message", fieldtype: "Small Text", label: __("Message to candidate (optional)") },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Internal reason (not sent)") },
		],
		primary_action_label: __("Reschedule"),
		primary_action(values) {
			run(values, 0)
				.then((r) => {
					d.hide();
					frappe.show_alert({
						message: r.message.changed ? __("Interview rescheduled") : __("No change in timing"),
						indicator: "green",
					});
					on_done && on_done();
				})
				.catch(() => grid_interview.offer_overlap(values, run, d, on_done));
		},
	});
	d.show();
};

// A SchedulingConflict can be overridden after explicit confirmation.
grid_interview.offer_overlap = (values, run, dialog, on_done) => {
	const last = frappe.last_response || {};
	if (!(last.exc_type || "").includes("SchedulingConflict")) return;
	frappe.confirm(__("Schedule anyway, despite the overlap?"), () =>
		run(values, 1).then(() => {
			dialog.hide();
			on_done && on_done();
		}),
	);
};

grid_interview.cancel_dialog = (session, on_done) => {
	const d = new frappe.ui.Dialog({
		title: __("Cancel Interview"),
		fields: [
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"The HRMS Interview is marked Cancelled and the candidate and interviewers are notified.",
				)}</p>`,
			},
			{ fieldname: "candidate_message", fieldtype: "Small Text", label: __("Message to candidate (optional)") },
			{ fieldname: "reason", fieldtype: "Small Text", label: __("Internal reason (not sent)"), reqd: 1 },
		],
		primary_action_label: __("Cancel Interview"),
		primary_action(values) {
			frappe
				.call({
					method: grid_interview.API + "cancel_interview",
					args: { video_session: session.name, ...values },
					freeze: true,
				})
				.then(() => {
					d.hide();
					frappe.show_alert({ message: __("Interview cancelled"), indicator: "orange" });
					on_done && on_done();
				});
		},
	});
	d.show();
};

grid_interview.show_notifications = (session_name) => {
	frappe.call({ method: grid_interview.API + "get_notifications", args: { video_session: session_name }, type: "GET" }).then((r) => {
		const rows = (r.message || [])
			.map(
				(n) => `<tr>
					<td>${frappe.utils.escape_html(n.notification_type.replace("interview_", ""))}</td>
					<td>${frappe.utils.escape_html(n.audience || "")}</td>
					<td>${frappe.utils.escape_html(n.recipient || "")}</td>
					<td>${frappe.utils.escape_html(n.status)}</td>
					<td>${n.attempts || 0}</td>
					<td class="text-muted small">${frappe.utils.escape_html(n.last_error || "")}</td>
					<td class="text-muted small">${frappe.datetime.str_to_user(n.creation)}</td>
				</tr>`,
			)
			.join("");
		frappe.msgprint({
			title: __("Notifications"),
			wide: true,
			message: rows
				? `<table class="table table-sm"><thead><tr><th>${__("Type")}</th><th>${__("Audience")}</th><th>${__(
						"Recipient",
				  )}</th><th>${__("Status")}</th><th>${__("Attempts")}</th><th>${__("Error")}</th><th>${__(
						"Created",
				  )}</th></tr></thead><tbody>${rows}</tbody></table>
				  <p class="small text-muted">${__(
						"Sent = accepted by the mail server. Inbox delivery is not confirmed.",
				  )}</p>`
				: __("No notifications yet."),
		});
	});
};
