// Candidate portal helpers. Every action calls a whitelisted API that re-checks access server-side.
window.gi = window.gi || {};

gi.form_values = (form) => {
	const out = {};
	new FormData(form).forEach((v, k) => (out[k] = typeof v === "string" ? v.trim() : v));
	form.querySelectorAll('input[type="checkbox"]').forEach((c) => (out[c.name] = c.checked ? 1 : 0));
	return out;
};

gi.message = (el, text, kind = "ok") => {
	el.className = `gi-alert ${kind}`;
	el.textContent = text;
	el.hidden = false;
};

gi.error_text = (r) => {
	try {
		const msgs = JSON.parse(r._server_messages || "[]").map((m) => JSON.parse(m).message);
		if (msgs.length) return msgs.map((m) => $("<div>").html(m).text()).join(" ");
	} catch (e) {
		/* fall through */
	}
	// our own exceptions are raised directly and arrive only as "exception" (text is user-facing)
	const m = /^grid_interview\.exceptions\.\w+: (.+)$/s.exec((r && r.exception) || "");
	return m ? m[1].trim() : __("Something went wrong. Please try again.");
};

// POST via frappe.call (adds the CSRF token); resolves with r.message, rejects with readable text.
gi.post = (method, args) =>
	new Promise((resolve, reject) => {
		frappe.call({
			method,
			args,
			type: "POST",
			callback: (r) => resolve(r.message),
			error: (r) => reject(gi.error_text(r.responseJSON || r)),
			freeze: true,
		});
	});

gi.bind_form = (form, method, { done, extra } = {}) => {
	const box = form.querySelector(".gi-alert") || form.parentElement.querySelector(".gi-alert");
	form.addEventListener("submit", (ev) => {
		ev.preventDefault();
		gi.post(method, { ...gi.form_values(form), ...(extra ? extra(form) : {}) })
			.then((m) => (done ? done(m, box) : gi.message(box, (m && m.message) || __("Saved"))))
			.catch((t) => gi.message(box, t, "err"));
	});
};

gi.upload = (form) => {
	const box = form.querySelector(".gi-alert");
	form.addEventListener("submit", (ev) => {
		ev.preventDefault();
		const data = new FormData(form);
		fetch("/api/method/grid_interview.api.portal.upload_document", {
			method: "POST",
			body: data,
			headers: { "X-Frappe-CSRF-Token": frappe.csrf_token, Accept: "application/json" },
			credentials: "same-origin",
		})
			.then((res) => res.json().then((j) => ({ ok: res.ok, j })))
			.then(({ ok, j }) => {
				if (!ok) throw gi.error_text(j);
				window.location.reload();
			})
			.catch((t) => gi.message(box, typeof t === "string" ? t : __("Upload failed."), "err"));
	});
};
