// Interview Scheduler: schedule standard HRMS Interviews and manage their Grid Interview sessions.

frappe.pages["interview-scheduler"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Interview Scheduler"), single_column: true });
	new GridInterviewScheduler(page);
};

class GridInterviewScheduler {
	constructor(page) {
		this.page = page;
		this.page.set_primary_action(__("Schedule Interview"), () => this.schedule_dialog(), "add");
		this.page.add_inner_button(__("Manage Desk Interviews"), () => this.attach_dialog());
		this.page.add_inner_button(__("Refresh"), () => this.refresh());

		this.filters = {
			from_date: this.page.add_field({
				fieldname: "from_date", label: __("From"), fieldtype: "Date",
				default: frappe.datetime.add_days(frappe.datetime.get_today(), -1),
				change: () => this.refresh(),
			}),
			to_date: this.page.add_field({
				fieldname: "to_date", label: __("To"), fieldtype: "Date",
				default: frappe.datetime.add_days(frappe.datetime.get_today(), 30),
				change: () => this.refresh(),
			}),
			status: this.page.add_field({
				fieldname: "status", label: __("Status"), fieldtype: "Select",
				options: ["", "Scheduled", "In Progress", "Completed", "No Show", "Cancelled", "Expired"].join("\n"),
				change: () => this.refresh(),
			}),
		};
		this.$body = $('<div class="gi-scheduler"></div>').appendTo(this.page.main);
		this.refresh();
	}

	refresh() {
		this.$body.html(`<div class="text-muted p-4">${__("Loading…")}</div>`);
		frappe
			.call({
				method: grid_interview.API + "get_overview",
				type: "GET",
				args: {
					from_date: this.filters.from_date.get_value(),
					to_date: this.filters.to_date.get_value(),
					status: this.filters.status.get_value(),
				},
			})
			.then((r) => this.render(r.message || []));
	}

	render(rows) {
		if (!rows.length) {
			this.$body.html(`<div class="text-muted p-4">${__("No interviews in this period.")}</div>`);
			return;
		}
		const e = frappe.utils.escape_html;
		const colour = { Scheduled: "blue", "In Progress": "orange", Completed: "green", Cancelled: "gray", Expired: "gray", "No Show": "red" };
		const body = rows
			.map((s, i) => {
				const n = s.notifications || {};
				const mail = ["Sent", "Queued", "Failed", "Skipped"]
					.filter((k) => n[k])
					.map((k) => `<span class="indicator-pill ${k === "Failed" ? "red" : k === "Sent" ? "green" : "gray"}">${__(k)} ${n[k]}</span>`)
					.join(" ");
				return `<tr data-idx="${i}">
					<td>${frappe.datetime.str_to_user(s.scheduled_start)}<div class="small text-muted">${__("until")} ${moment(s.scheduled_end).format("HH:mm")}</div></td>
					<td><b>${e(s.applicant_name || s.job_applicant)}</b><div class="small text-muted">${e(s.job_title || "")}</div></td>
					<td>${e(s.interview_type)}</td>
					<td class="small">${s.interviewers.map(e).join("<br>")}</td>
					<td><span class="indicator-pill ${colour[s.status] || "gray"}">${__(s.status)}</span></td>
					<td>${mail || '<span class="text-muted small">—</span>'}</td>
					<td class="text-right"><button class="btn btn-xs btn-default gi-actions">${__("Actions")}</button></td>
				</tr>`;
			})
			.join("");
		this.$body.html(`<div class="table-responsive"><table class="table table-hover">
			<thead><tr><th>${__("When")}</th><th>${__("Candidate")}</th><th>${__("Round")}</th><th>${__("Interviewers")}</th>
			<th>${__("Status")}</th><th>${__("Emails")}</th><th></th></tr></thead><tbody>${body}</tbody></table></div>`);
		this.$body.find(".gi-actions").on("click", (ev) => this.actions(rows[$(ev.target).closest("tr").data("idx")], ev.target));
	}

	actions(s, target) {
		const items = [{ label: __("Open"), action: () => frappe.set_route("Form", "Grid Interview Video Session", s.name) }];
		if (["Scheduled", "In Progress"].includes(s.status))
			items.push({
				label: __("Open Interview Room"),
				action: () => window.open(`/interview/room?session=${encodeURIComponent(s.name)}`, "_blank", "noopener"),
			});
		if (["Scheduled", "Expired", "No Show"].includes(s.status))
			items.push({ label: __("Reschedule"), action: () => grid_interview.reschedule_dialog(s, () => this.refresh()) });
		if (s.status === "Scheduled")
			items.push({
				label: __("Resend Invitation"),
				action: () =>
					frappe
						.call({ method: grid_interview.API + "resend_invitation", args: { video_session: s.name } })
						.then(() => this.refresh()),
			});
		if (!["Cancelled", "Completed", "In Progress"].includes(s.status))
			items.push({ label: __("Cancel"), action: () => grid_interview.cancel_dialog(s, () => this.refresh()) });
		items.push({ label: __("Notifications"), action: () => grid_interview.show_notifications(s.name) });
		items.push({ label: __("HRMS Interview"), action: () => frappe.set_route("Form", "Interview", s.interview) });

		const $menu = $(`<div class="dropdown-menu show" style="position:absolute;z-index:1000"></div>`);
		items.forEach((it) => $(`<a class="dropdown-item" href="#">${it.label}</a>`).on("click", (ev) => {
			ev.preventDefault();
			$menu.remove();
			it.action();
		}).appendTo($menu));
		$(".gi-scheduler .dropdown-menu").remove();
		$menu.appendTo(this.$body).css({ top: $(target).position().top + 30, right: 20 });
		setTimeout(() => $(document).one("click", () => $menu.remove()), 0);
	}

	schedule_dialog() {
		const run = (values, allow_overlap) =>
			frappe.call({
				method: grid_interview.API + "schedule_interview",
				args: { ...values, interviewers: JSON.stringify(values.interviewers || []), allow_overlap },
				freeze: true,
			});
		const d = new frappe.ui.Dialog({
			title: __("Schedule Interview"),
			size: "large",
			fields: [
				{ fieldname: "job_applicant", fieldtype: "Link", options: "Job Applicant", label: __("Candidate (Job Applicant)"), reqd: 1 },
				{ fieldname: "interview_type", fieldtype: "Link", options: "Interview Type", label: __("Interview Round (Interview Type)"), reqd: 1 },
				{
					fieldname: "interviewers", fieldtype: "MultiSelectPills", label: __("Interviewers"), reqd: 1,
					get_data: (txt) =>
						frappe.call({ method: grid_interview.API + "search_interviewers", args: { txt }, type: "GET" }).then((r) => r.message),
				},
				{ fieldtype: "Column Break" },
				{ fieldname: "scheduled_on", fieldtype: "Date", label: __("Date"), reqd: 1 },
				{ fieldname: "from_time", fieldtype: "Time", label: __("From"), reqd: 1 },
				{ fieldname: "to_time", fieldtype: "Time", label: __("To"), reqd: 1 },
				{ fieldtype: "Section Break" },
				{ fieldname: "instructions", fieldtype: "Small Text", label: __("Instructions for the candidate") },
				{
					fieldname: "recording_eligible", fieldtype: "Check", label: __("Recording eligible"),
					description: __("Recording also needs consent from every participant and must be enabled in settings."),
				},
			],
			primary_action_label: __("Schedule and Notify"),
			primary_action: (values) => {
				run(values, 0)
					.then((r) => {
						d.hide();
						frappe.show_alert({ message: __("Interview {0} scheduled", [r.message.interview]), indicator: "green" });
						this.refresh();
					})
					.catch(() => grid_interview.offer_overlap(values, run, d, () => this.refresh()));
			},
		});
		d.show();
	}

	attach_dialog() {
		frappe.call({ method: grid_interview.API + "get_unmanaged_interviews", type: "GET" }).then((r) => {
			const rows = r.message || [];
			if (!rows.length) {
				frappe.msgprint(__("All pending future interviews are already managed by Grid Interview."));
				return;
			}
			const d = new frappe.ui.Dialog({
				title: __("Manage an interview created in HRMS"),
				fields: [
					{
						fieldname: "interview", fieldtype: "Select", label: __("Interview"), reqd: 1,
						options: rows.map((i) => ({
							value: i.name,
							label: `${i.name} · ${i.job_applicant} · ${i.interview_type} · ${frappe.datetime.str_to_user(i.scheduled_on)} ${i.from_time}`,
						})),
					},
					{ fieldname: "instructions", fieldtype: "Small Text", label: __("Instructions for the candidate") },
					{ fieldname: "recording_eligible", fieldtype: "Check", label: __("Recording eligible") },
				],
				primary_action_label: __("Manage and Notify"),
				primary_action: (values) =>
					frappe
						.call({ method: grid_interview.API + "attach_interview", args: values, freeze: true })
						.then(() => {
							d.hide();
							this.refresh();
						}),
			});
			d.show();
		});
	}
}
