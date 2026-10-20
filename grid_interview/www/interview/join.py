import frappe
from frappe import _
from frappe.sessions import get_csrf_token

no_cache = 1
no_sitemap = 1


def get_context(context):
	# Nothing is validated or redeemed on GET: email security scanners open links
	# automatically, and a GET must not bind the link to their "browser".
	context.no_cache = 1
	context.show_sidebar = False
	context.title = _("Join your interview")
	context.token = frappe.form_dict.get("t") or ""
	if frappe.session.user != "Guest":
		get_csrf_token()  # a signed-in browser's POST needs Frappe's CSRF token (see room.py)
	return context
