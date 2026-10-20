// Interview room: device check, embedded meeting (Jitsi IFrame API or Mock), heartbeat,
// reconnect and moderator controls. The server decides access, moderator rights and timing;
// this file only renders what /api/method/grid_interview.api.video.* returns.
(() => {
	const root = document.getElementById("gi-room");
	if (!root || !root.dataset.session) return;
	const SESSION = root.dataset.session;
	const API = "/api/method/grid_interview.api.video.";
	const $id = (id) => document.getElementById(id);
	const esc = (v) =>
		String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
	if (typeof window.__ !== "function") window.__ = (s, args) => (args || []).reduce((t, a, i) => t.replace(`{${i}}`, a), s);
	const state = { info: null, config: null, api: null, preview: null, timer: null, joined: false, ending: false };

	// frappe.throw fills _server_messages; our own exceptions (OutsideJoinWindow, AccessDenied …) are
	// raised directly and only arrive as "exception". Their text is written for users; nothing else is shown.
	function errorText(data, fallback) {
		try {
			const text = $("<div>").html(JSON.parse(JSON.parse(data._server_messages)[0]).message).text();
			if (text) return text;
		} catch (e) {
			/* try the exception below */
		}
		const m = /^grid_interview\.exceptions\.\w+: (.+)$/s.exec((data && data.exception) || "");
		return m ? m[1].trim() : fallback || __("Something went wrong. Please try again.");
	}

	function headers() {
		const h = { Accept: "application/json", "Content-Type": "application/json", "X-Grid-Interview": "1" };
		if (frappe.csrf_token && frappe.csrf_token !== "None") h["X-Frappe-CSRF-Token"] = frappe.csrf_token;
		return h;
	}

	async function call(method, { post = false } = {}) {
		const url = API + method + (post ? "" : "?video_session=" + encodeURIComponent(SESSION));
		const res = await fetch(url, {
			method: post ? "POST" : "GET",
			credentials: "same-origin",
			headers: headers(),
			body: post ? JSON.stringify({ video_session: SESSION }) : undefined,
		});
		const data = await res.json().catch(() => ({}));
		if (!res.ok) {
			const err = new Error(errorText(data));
			err.status = res.status;
			throw err;
		}
		return data.message;
	}

	const REC_API = "/api/method/grid_interview.api.recording.";

	async function recCall(method, body) {
		const res = await fetch(REC_API + method + (body ? "" : "?video_session=" + encodeURIComponent(SESSION)), {
			method: body ? "POST" : "GET",
			credentials: "same-origin",
			headers: headers(),
			body: body ? JSON.stringify({ video_session: SESSION, ...body }) : undefined,
		});
		const data = await res.json().catch(() => ({}));
		if (!res.ok) throw new Error(errorText(data));
		return data.message;
	}

	// ------------------------------------------------------------------ consent + recording
	function renderConsent(c) {
		$id("gi-consent-rec").checked = !!c.recording;
		$id("gi-consent-ext").checked = !!c.external_processing;
		updateRecording(c);
	}

	async function setupConsent(info) {
		$id("gi-consent").hidden = false;
		$id("gi-consent-text").innerHTML = info.consent_text || "";
		$id("gi-consent-ext-row").hidden = !info.external_processing_offered;
		const send = (type, el) =>
			recCall("set_consent", { consent_type: type, granted: el.checked ? 1 : 0 })
				.then(renderConsent)
				.catch((e) => {
					el.checked = !el.checked;
					message(e.message);
				});
		$id("gi-consent-rec").addEventListener("change", (ev) => send("Recording", ev.target));
		$id("gi-consent-ext").addEventListener("change", (ev) => send("External Processing", ev.target));
		if (info.moderator) $id("gi-rec-controls").hidden = false;
		try {
			renderConsent(await recCall("consent_status"));
		} catch (e) {
			message(e.message);
		}
	}

	function updateRecording(r) {
		const live = !!r.recording_live;
		const pill = $id("gi-rec");
		if (state.info && state.info.may_be_recorded) {
			pill.hidden = false;
			pill.textContent = live ? "● " + __("Recording") : __("Not recording");
			pill.className = `gi-pill ${live ? "red" : "gray"}`;
		}
		if (state.info && state.info.moderator) {
			$id("gi-rec-start").hidden = live;
			$id("gi-rec-stop").hidden = !live;
			if (r.start_blockers) $id("gi-rec-blockers").innerHTML = r.start_blockers.map((b) => `<div>${esc(b)}</div>`).join("");
			// the server is the source of truth: stop the provider recording if it is no longer approved
			if (state.recording && !live) stopProviderRecording();
		}
	}

	function stopProviderRecording() {
		if (state.api && state.recording) state.api.executeCommand("stopRecording", "file");
		state.recording = false;
	}

	async function startRecording() {
		try {
			await recCall("start_recording", {});
		} catch (e) {
			message(e.message);
			recCall("consent_status").then(updateRecording).catch(() => {});
			return;
		}
		if (state.api) state.api.executeCommand("startRecording", { mode: "file" });
		state.recording = true;
		updateRecording({ recording_live: true });
	}

	async function stopRecording() {
		stopProviderRecording();
		try {
			await recCall("stop_recording", {});
		} catch (e) {
			message(e.message);
		}
		updateRecording({ recording_live: false });
	}

	function message(text, kind = "err") {
		const box = $id("gi-room-msg");
		if (!text) return (box.hidden = true);
		box.className = `gi-alert ${kind}`;
		box.textContent = text;
		box.hidden = false;
	}

	function conn(text, colour) {
		const el = $id("gi-conn");
		el.textContent = text;
		el.className = `gi-pill ${colour}`;
	}

	// ------------------------------------------------------------------ details panel
	function renderDetails(info) {
		$id("gi-room-title").textContent = info.job_title ? `${__("Interview")}: ${info.job_title}` : __("Interview");
		const when = `${info.start_label} – ${info.end_label} (${info.timezone})`;
		$id("gi-room-sub").textContent = `${info.round || ""} · ${when}`;
		$id("gi-window").textContent = __("Joining is possible from {0} until {1} ({2}).", [info.opens_label, info.closes_label, info.timezone]);
		$id("gi-recording-notice").hidden = !info.may_be_recorded;
		$id("gi-details").innerHTML = `<h3>${__("Details")}</h3><dl>
			<dt>${__("Round")}</dt><dd>${esc(info.round)}</dd>
			<dt>${__("Scheduled")}</dt><dd>${esc(when)}</dd>
			<dt>${__("You are joining as")}</dt><dd>${esc(info.display_name)} (${esc(__(info.participant_kind))})</dd>
			${info.instructions ? `<dt>${__("Instructions")}</dt><dd style="white-space:pre-line">${esc(info.instructions)}</dd>` : ""}
		</dl>`;
		if (info.moderator) {
			const skills = (info.expected_skills || []).map((s) => `<li>${esc(s.skill)}${s.description ? ` – <span class="gi-meta">${esc(s.description)}</span>` : ""}</li>`).join("");
			$id("gi-moderator").innerHTML = `<h3>${__("Candidate")}</h3><dl>
				<dt>${__("Name")}</dt><dd>${esc(info.candidate_name)}</dd>
				<dt>${__("Interviewers")}</dt><dd>${(info.interviewers || []).map(esc).join(", ")}</dd>
				<dt>${__("Skills to assess")}</dt><dd>${skills ? `<ul>${skills}</ul>` : `<span class="gi-meta">${__("None defined on the Interview Type")}</span>`}</dd>
				<dt>${__("Records")}</dt><dd>
					<a href="/desk/job-applicant/${encodeURIComponent(info.job_applicant)}" target="_blank" rel="noopener">${__("Job Applicant / resume")}</a><br>
					<a href="/desk/interview/${encodeURIComponent(info.interview)}" target="_blank" rel="noopener">${__("HRMS Interview")}</a>
				</dd></dl>
				${info.participant_kind === "Interviewer" ? `<a class="btn btn-default btn-sm" target="_blank" rel="noopener"
					href="/desk/interview-feedback/new?interview=${encodeURIComponent(info.interview)}">${__("Give feedback (HRMS)")}</a>` : ""}`;
			$id("gi-moderator").hidden = false;
			$id("gi-end").hidden = false;
		}
	}

	// ------------------------------------------------------------------ device check
	async function startPreview() {
		const cam = $id("gi-cam");
		const mic = $id("gi-mic");
		if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
			cam.textContent = __("Camera: not supported by this browser");
			mic.textContent = __("Microphone: not supported by this browser");
			cam.className = mic.className = "gi-pill red";
			return;
		}
		try {
			state.preview = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
			$id("gi-preview").srcObject = state.preview;
			const hasVideo = state.preview.getVideoTracks().length > 0;
			const hasAudio = state.preview.getAudioTracks().length > 0;
			cam.textContent = hasVideo ? __("Camera: ready") : __("Camera: not found");
			mic.textContent = hasAudio ? __("Microphone: ready") : __("Microphone: not found");
			cam.className = `gi-pill ${hasVideo ? "green" : "red"}`;
			mic.className = `gi-pill ${hasAudio ? "green" : "red"}`;
		} catch (e) {
			cam.textContent = __("Camera: permission denied or in use");
			mic.textContent = __("Microphone: permission denied or in use");
			cam.className = mic.className = "gi-pill red";
			message(__("Allow camera and microphone access in your browser settings, then reload this page."), "info");
		}
	}

	function stopPreview() {
		if (state.preview) state.preview.getTracks().forEach((t) => t.stop());
		state.preview = null;
	}

	// ------------------------------------------------------------------ join / leave
	async function join() {
		message("");
		$id("gi-join").disabled = true;
		conn(__("Connecting…"), "orange");
		try {
			state.config = await call("join", { post: true });
		} catch (e) {
			conn(__("Not connected"), "gray");
			message(e.message);
			$id("gi-join").disabled = false;
			return;
		}
		stopPreview();
		$id("gi-prejoin").hidden = true;
		$id("gi-left").hidden = true;
		$id("gi-actions").hidden = false;
		if (state.config.provider === "Jitsi") await openJitsi(state.config);
		else await openMock();
		state.joined = true;
		conn(__("Connected"), "green");
		startHeartbeat();
	}

	function loadScript(src) {
		return new Promise((resolve, reject) => {
			if (window.JitsiMeetExternalAPI) return resolve();
			const s = document.createElement("script");
			s.src = src;
			s.onload = resolve;
			s.onerror = () => reject(new Error(__("The video service could not be reached. Check your connection and try again.")));
			document.head.appendChild(s);
		});
	}

	async function openJitsi(cfg) {
		try {
			await loadScript(cfg.external_api);
		} catch (e) {
			message(e.message);
			return;
		}
		const node = $id("gi-meeting");
		node.hidden = false;
		node.innerHTML = "";
		state.api = new window.JitsiMeetExternalAPI(cfg.domain, {
			roomName: cfg.room,
			jwt: cfg.jwt,
			parentNode: node,
			width: "100%",
			height: "100%",
			userInfo: { displayName: cfg.display_name },
			configOverwrite: {
				prejoinConfig: { enabled: false },
				disableDeepLinking: true,
				enableWelcomePage: false,
				enableClosePage: false,
				disableInviteFunctions: true,
				liveStreamingEnabled: false,
				hideConferenceSubject: true,
				// no "recording" button: recording is started only through Grid Interview after consent
				toolbarButtons: ["microphone", "camera", "desktop", "chat", "raisehand", "tileview",
					"participants-pane", "settings", "fullscreen", "hangup"],
			},
			interfaceConfigOverwrite: { SHOW_JITSI_WATERMARK: false, SHOW_BRAND_WATERMARK: false, MOBILE_APP_PROMO: false },
		});
		state.api.addListener("videoConferenceJoined", () => {
			conn(__("Connected"), "green");
			if (cfg.moderator && cfg.enable_lobby) state.api.executeCommand("toggleLobby", true);
		});
		state.api.addListener("videoConferenceLeft", () => left(__("You have left the interview")));
		state.api.addListener("readyToClose", () => left(__("You have left the interview")));
		state.api.addListener("errorOccurred", (e) => {
			if (e && e.error && e.error.isFatal) message(__("The video connection failed. Use Rejoin to try again."));
		});
	}

	async function openMock() {
		$id("gi-mock").hidden = false;
		try {
			state.preview = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
			$id("gi-mock-video").srcObject = state.preview;
		} catch (e) {
			/* the mock room works without a camera */
		}
	}

	function teardown() {
		stopHeartbeat();
		if (state.api) {
			try {
				state.api.dispose();
			} catch (e) {
				/* ignore */
			}
			state.api = null;
		}
		stopPreview();
		$id("gi-meeting").hidden = true;
		$id("gi-mock").hidden = true;
		$id("gi-actions").hidden = true;
		state.joined = false;
	}

	async function left(title, text, allowRejoin = true) {
		if (!state.joined) return;
		teardown();
		call("leave", { post: true }).catch(() => {});
		conn(__("Not connected"), "gray");
		$id("gi-left-title").textContent = title;
		if (text) $id("gi-left-text").textContent = text;
		$id("gi-rejoin").hidden = !allowRejoin;
		$id("gi-left").hidden = false;
	}

	function ended(status) {
		teardown();
		conn(__(status), "gray");
		$id("gi-left-title").textContent = __("The interview has ended");
		$id("gi-left-text").textContent = __("Status: {0}. You can close this page.", [__(status)]);
		$id("gi-rejoin").hidden = true;
		$id("gi-left").hidden = false;
	}

	// ------------------------------------------------------------------ heartbeat / reconnect
	function startHeartbeat() {
		stopHeartbeat();
		const every = ((state.config && state.config.heartbeat_seconds) || 30) * 1000;
		state.timer = setInterval(async () => {
			try {
				const r = await call("heartbeat", { post: true });
				if (!r.active) return ended(r.status);
				conn(__("Connected"), "green");
				updateRecording(r);
			} catch (e) {
				if (e.status === 403 || e.status === 417) return ended(__("Closed"));
				conn(__("Reconnecting…"), "orange");
			}
		}, every);
	}

	function stopHeartbeat() {
		if (state.timer) clearInterval(state.timer);
		state.timer = null;
	}

	window.addEventListener("offline", () => state.joined && conn(__("Offline – waiting for network"), "red"));
	window.addEventListener("online", () => {
		if (!state.joined) return;
		// re-register with the server (logged as a reconnect); Jitsi reconnects media itself
		call("join", { post: true }).then(() => conn(__("Connected"), "green")).catch((e) => message(e.message));
	});

	// ------------------------------------------------------------------ wiring
	$id("gi-join").addEventListener("click", join);
	$id("gi-rec-start").addEventListener("click", () => {
		if (confirm(__("Start recording? Everyone sees the recording indicator."))) startRecording();
	});
	$id("gi-rec-stop").addEventListener("click", stopRecording);
	$id("gi-rejoin").addEventListener("click", () => {
		$id("gi-left").hidden = true;
		$id("gi-prejoin").hidden = false;
		startPreview();
	});
	$id("gi-leave").addEventListener("click", () => {
		if (state.api) state.api.executeCommand("hangup");
		left(__("You have left the interview"));
	});
	$id("gi-end").addEventListener("click", () => {
		if (!confirm(__("End the interview for everyone? This marks it Completed if the candidate joined."))) return;
		fetch(API + "end_interview", {
			method: "POST",
			credentials: "same-origin",
			headers: headers(),
			body: JSON.stringify({ video_session: SESSION }),
		})
			.then((r) => r.json().then((d) => ({ ok: r.ok, d })))
			.then(({ ok, d }) => {
				if (!ok) return message(errorText(d, __("Could not end the interview.")));
				if (state.api) state.api.executeCommand("endConference");
				ended(d.message.status);
			});
	});
	window.addEventListener("beforeunload", () => stopHeartbeat());

	call("room_info")
		.then((info) => {
			state.info = info;
			renderDetails(info);
			if (["Completed", "Cancelled", "Expired", "No Show"].includes(info.status)) return ended(info.status);
			if (info.may_be_recorded) setupConsent(info);
			startPreview();
		})
		.catch((e) => {
			message(e.message);
			$id("gi-join").hidden = true;
		});
})();
