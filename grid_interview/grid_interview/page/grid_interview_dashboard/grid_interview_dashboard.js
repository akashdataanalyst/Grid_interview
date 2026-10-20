// Interview Dashboard: counts per stage, filtered lists, candidate timeline and HR decisions.
// All data is permission-filtered on the server (interviewers see only their own interviews).

frappe.pages["grid-interview-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Interview Dashboard"), single_column: true });
	new GridInterviewDashboard(page);
};

// [bucket, label, colour, group]; a red tile is highlighted only when its count is above zero.
const GI_TILES = [
	["today", __("Today"), "blue", "interviews"],
	["upcoming", __("Upcoming"), "blue", "interviews"],
	["in_progress", __("In progress"), "orange", "interviews"],
	["completed", __("Completed"), "green", "interviews"],
	["feedback_pending", __("Feedback pending"), "orange", "interviews"],
	["awaiting_decision", __("Awaiting decision"), "orange", "interviews"],
	["processing", __("Recordings processing"), "gray", "processing"],
	["review_pending", __("AI drafts to review"), "purple", "processing"],
	["transcription_failed", __("Transcription failures"), "red", "processing"],
	["analysis_failed", __("AI failures"), "red", "processing"],
];

const GI_GROUPS = [
	["interviews", __("Interviews")],
	["processing", __("Recording & AI")],
];

// auto-fit (not auto-fill) so a group's tiles always stretch across the full width.
const GI_STYLE = `
	.gi-dash { padding: 20px 24px 32px; }
	.gi-section { margin-bottom: 24px; }
	.gi-group-label { margin: 0 0 10px; font-size: var(--text-xs); color: var(--text-muted); font-weight: 600;
		text-transform: uppercase; letter-spacing: 0.04em; }
	.gi-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; }
	.gi-tile { position: relative; display: flex; flex-direction: column; justify-content: space-between; min-height: 96px;
		padding: 14px 16px 14px 20px; border-radius: var(--border-radius-lg); border: 1px solid var(--border-color);
		background: var(--card-bg); box-shadow: var(--shadow-xs); text-decoration: none !important;
		transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s; overflow: hidden; }
	.gi-tile::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--gi-accent); }
	.gi-tile:hover { border-color: var(--gray-400); box-shadow: var(--shadow-sm); transform: translateY(-1px); }
	.gi-tile.active { border-color: var(--gi-accent); box-shadow: 0 0 0 1px var(--gi-accent), var(--shadow-sm); }
	.gi-tile .gi-label { font-size: var(--text-sm); color: var(--text-muted); font-weight: 500; line-height: 1.3; }
	.gi-tile .gi-count { font-size: 2rem; font-weight: 700; line-height: 1; margin-top: 12px; color: var(--text-color);
		font-variant-numeric: tabular-nums; }
	.gi-tile.gi-alert .gi-count { color: var(--red-500); }
	.gi-tile[data-colour="blue"] { --gi-accent: var(--blue-500); }
	.gi-tile[data-colour="orange"] { --gi-accent: var(--orange-500); }
	.gi-tile[data-colour="green"] { --gi-accent: var(--green-500); }
	.gi-tile[data-colour="purple"] { --gi-accent: var(--purple-500); }
	.gi-tile[data-colour="red"] { --gi-accent: var(--red-500); }
	.gi-tile[data-colour="gray"] { --gi-accent: var(--gray-500); }
	.gi-card { border: 1px solid var(--border-color); border-radius: var(--border-radius-lg); background: var(--card-bg);
		box-shadow: var(--shadow-xs); }
	.gi-list { position: relative; }
	.gi-card-head { display: flex; align-items: center; gap: 8px; padding: 12px 16px; border-bottom: 1px solid var(--border-color); }
	.gi-card-head h5 { margin: 0; font-size: var(--text-base); font-weight: 600; }
	.gi-card .table { margin: 0; }
	.gi-card .table th { font-size: var(--text-xs); color: var(--text-muted); font-weight: 600; text-transform: uppercase;
		letter-spacing: 0.03em; border-top: 0; padding: 10px 16px; }
	.gi-card .table td { padding: 10px 16px; vertical-align: middle; }
	.gi-empty { padding: 40px 16px; text-align: center; color: var(--text-muted); }
	@media (max-width: 767px) { .gi-dash { padding: 12px; } .gi-tiles { grid-template-columns: repeat(2, 1fr); } }
`;

class GridInterviewDashboard {
	constructor(page) {
		this.page = page;
		this.api = "grid_interview.api.dashboard.";
		this.bucket = "today";
		this.page.add_inner_button(__("Interview Scheduler"), () => frappe.set_route("interview-scheduler"));
		this.page.add_inner_button(__("Candidate Timeline"), () => this.timeline_prompt());
		this.page.add_inner_button(__("Refresh"), () => this.refresh());
		const today = frappe.datetime.get_today();
		const field = (df) => this.page.add_field({ ...df, change: () => this.refresh() });
		this.filters = {
			from_date: field({ fieldname: "from_date", label: __("From"), fieldtype: "Date", default: frappe.datetime.add_days(today, -30) }),
			to_date: field({ fieldname: "to_date", label: __("To"), fieldtype: "Date", default: frappe.datetime.add_days(today, 30) }),
			job_opening: field({ fieldname: "job_opening", label: __("Job Opening"), fieldtype: "Link", options: "Job Opening" }),
			department: field({ fieldname: "department", label: __("Department"), fieldtype: "Link", options: "Department" }),
			interview_type: field({ fieldname: "interview_type", label: __("Round"), fieldtype: "Link", options: "Interview Type" }),
			interviewer: field({ fieldname: "interviewer", label: __("Interviewer"), fieldtype: "Link", options: "User" }),
		};
		$(`<style>${GI_STYLE}</style>`).appendTo(this.page.main);
		const $dash = $('<div class="gi-dash"></div>').appendTo(this.page.main);
		this.$tiles = $('<div class="gi-tile-groups"></div>').appendTo($dash);
		const $card = $('<div class="gi-card"><div class="gi-card-head"><h5></h5><span class="gi-badge"></span></div></div>').appendTo($dash);
		this.$title = $card.find("h5");
		this.$badge = $card.find(".gi-badge");
		this.$list = $('<div class="gi-list"></div>').appendTo($card);
		this.refresh();
	}

	values() {
		const out = {};
		Object.entries(this.filters).forEach(([k, f]) => {
			const v = f.get_value();
			if (v) out[k] = v;
		});
		return out;
	}

	refresh() {
		frappe.call({ method: this.api + "get_summary", args: { filters: this.values() }, type: "GET" }).then((r) => {
			const counts = r.message.counts;
			const tile = ([key, label, colour]) => {
				const classes = ["gi-tile"];
				if (key === this.bucket) classes.push("active");
				if (counts[key] && colour === "red") classes.push("gi-alert");
				return `<a href="#" class="${classes.join(" ")}" data-bucket="${key}" data-colour="${colour}">
					<div class="gi-label">${label}</div>
					<div class="gi-count">${counts[key] || 0}</div>
				</a>`;
			};
			this.$tiles.html(
				GI_GROUPS.map(
					([group, title]) => `<div class="gi-section"><div class="gi-group-label">${title}</div>
						<div class="gi-tiles">${GI_TILES.filter((t) => t[3] === group).map(tile).join("")}</div></div>`,
				).join(""),
			);
			this.$tiles.find(".gi-tile").on("click", (ev) => {
				ev.preventDefault();
				this.bucket = $(ev.currentTarget).data("bucket");
				this.refresh();
			});
			this.load_list();
		});
	}

	load_list() {
		const label = (GI_TILES.find((t) => t[0] === this.bucket) || [null, __("All")])[1];
		this.$title.text(label);
		this.$badge.empty();
		this.$list.html(`<div class="gi-empty">${__("Loading…")}</div>`);
		frappe
			.call({ method: this.api + "get_list", args: { bucket: this.bucket, filters: this.values() }, type: "GET" })
			.then((r) => this.render(r.message || []));
	}

	render(rows) {
		this.$badge.html(`<span class="indicator-pill gray">${rows.length}</span>`);
		if (!rows.length) {
			this.$list.html(`<div class="gi-empty">${__("No interviews in this list for the selected filters.")}</div>`);
			return;
		}
		const e = frappe.utils.escape_html;
		const body = rows
			.map(
				(s, i) => `<tr data-idx="${i}">
				<td>${frappe.datetime.str_to_user(s.scheduled_start)}</td>
				<td><a href="#" class="gi-tl">${e(s.applicant_name || s.job_applicant)}</a><div class="small text-muted">${e(s.job_title || s.designation || "")}</div></td>
				<td>${e(s.interview_type)}</td>
				<td>${e(__(s.status))}<div class="small text-muted">${__("HRMS")}: ${e(__(s.interview_status))}</div></td>
				<td>${s.feedback_submitted}/${s.feedback_expected}</td>
				<td class="small">${e(__(s.processing_status || ""))}${s.review_status ? `<br>${__("AI")}: ${e(__(s.review_status))}` : ""}</td>
				<td class="text-right"><button class="btn btn-xs btn-default gi-act">${__("Actions")}</button></td>
			</tr>`,
			)
			.join("");
		this.$list.html(`<div class="table-responsive"><table class="table table-hover table-sm">
			<thead><tr><th>${__("When")}</th><th>${__("Candidate")}</th><th>${__("Round")}</th><th>${__("Status")}</th>
			<th>${__("Feedback")}</th><th>${__("Processing")}</th><th></th></tr></thead><tbody>${body}</tbody></table></div>`);
		const row = (ev) => rows[$(ev.target).closest("tr").data("idx")];
		this.$list.find(".gi-tl").on("click", (ev) => {
			ev.preventDefault();
			this.show_timeline(row(ev).job_applicant);
		});
		this.$list.find(".gi-act").on("click", (ev) => this.actions(row(ev), ev.target));
	}

	actions(s, target) {
		const menu = [
			[__("Open session"), () => frappe.set_route("Form", "Grid Interview Video Session", s.name)],
			[__("HRMS Interview"), () => frappe.set_route("Form", "Interview", s.interview)],
			[__("Give feedback"), () =>
				frappe.new_doc("Interview Feedback", { interview: s.interview, interviewer: frappe.session.user })],
		];
		if (s.analysis) menu.push([__("AI draft"), () => frappe.set_route("Form", "Grid Interview Analysis", s.analysis)]);
		if (["Completed", "No Show"].includes(s.status) && s.interview_docstatus === 0)
			menu.push([__("Record decision"), () => this.decision_dialog(s)]);
		menu.push([__("Candidate timeline"), () => this.show_timeline(s.job_applicant)]);
		const $m = $('<div class="dropdown-menu show" style="position:absolute;z-index:1000"></div>');
		menu.forEach(([label, fn]) =>
			$(`<a class="dropdown-item" href="#"></a>`).text(label).on("click", (ev) => {
				ev.preventDefault();
				$m.remove();
				fn();
			}).appendTo($m),
		);
		$(".gi-list .dropdown-menu").remove();
		$m.appendTo(this.$list).css({ top: $(target).position().top + 30, right: 20 });
		setTimeout(() => $(document).one("click", () => $m.remove()), 0);
	}

	decision_dialog(s) {
		const send = (values, override) =>
			frappe.call({
				method: this.api + "record_decision",
				args: { video_session: s.name, ...values, override_missing_feedback: override },
				freeze: true,
			});
		const d = new frappe.ui.Dialog({
			title: __("Record decision for {0}", [s.applicant_name || s.job_applicant]),
			fields: [
				{ fieldtype: "HTML", options: `<p class="text-muted small">${__(
					"Sets the HRMS Interview status. Nothing is shown to the candidate until you publish a Grid Interview Result, and the Job Applicant is not changed automatically. The AI draft is decision support only.",
				)}</p>` },
				{ fieldname: "decision", fieldtype: "Select", label: __("Decision"), reqd: 1, options: "\nCleared\nRejected\nUnder Review" },
				{ fieldname: "summary", fieldtype: "Small Text", label: __("Interview summary (internal)") },
				{ fieldname: "submit", fieldtype: "Check", label: __("Submit the HRMS Interview (final)"), depends_on: "eval:['Cleared','Rejected'].includes(doc.decision)" },
			],
			primary_action_label: __("Save decision"),
			primary_action: (values) =>
				send(values, 0)
					.then(() => {
						d.hide();
						this.refresh();
					})
					.catch(() => {
						const last = frappe.last_response || {};
						if (!(last._server_messages || "").includes("Feedback")) return;
						frappe.confirm(__("Record the decision even though feedback is missing?"), () =>
							send(values, 1).then(() => {
								d.hide();
								this.refresh();
							}),
						);
					}),
		});
		d.show();
	}

	timeline_prompt() {
		frappe.prompt({ fieldname: "job_applicant", fieldtype: "Link", options: "Job Applicant", label: __("Candidate"), reqd: 1 },
			(v) => this.show_timeline(v.job_applicant), __("Candidate Timeline"));
	}

	show_timeline(job_applicant) {
		frappe.call({ method: this.api + "get_timeline", args: { job_applicant }, type: "GET" }).then((r) => {
			const e = frappe.utils.escape_html;
			const items = (r.message || [])
				.map((ev) => {
					const link = ev.link ? `<a href="/desk/${frappe.router.slug(ev.link[0])}/${encodeURIComponent(ev.link[1])}">${__("open")}</a>` : "";
					return `<li style="margin-bottom:8px"><span class="text-muted small">${frappe.datetime.str_to_user(ev.time)}</span>
						<span class="indicator-pill gray" style="margin:0 6px">${e(__(ev.kind))}</span>${e(ev.title)}
						${ev.detail ? `<span class="text-muted small"> (${e(__(ev.detail))})</span>` : ""} ${link}</li>`;
				})
				.join("");
			frappe.msgprint({ title: __("Candidate timeline"), wide: true, message: `<ul style="list-style:none;padding:0">${items}</ul>` });
		});
	}
}
