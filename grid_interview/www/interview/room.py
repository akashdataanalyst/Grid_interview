import frappe
from frappe import _
from frappe.sessions import get_csrf_token

from grid_interview.services import meeting
from grid_interview.www_helpers import redirect

no_cache = 1
no_sitemap = 1


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	context.no_header = 1
	context.title = _("Interview room")
	session = (frappe.form_dict.get("session") or "").strip()
	context.session_name = session
	if not session or not frappe.db.exists("Grid Interview Video Session", session):
		context.error = _("Interview not found.")
		return context
	if frappe.session.user == "Guest":
		if not meeting.access_from_cookie(session):
			from urllib.parse import quote

			redirect("/login?redirect-to=" + quote(f"/interview/room?session={session}", safe=""))
	else:
		get_csrf_token()  # make sure the page carries a CSRF token (see www_helpers)
	return context
