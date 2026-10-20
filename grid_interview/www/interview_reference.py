# Route /interview-reference (Frappe maps hyphens in the page name to underscores in the module name).
import frappe
from frappe import _

from grid_interview.exceptions import InvalidAccessToken
from grid_interview.services import references

no_cache = 1
no_sitemap = 1


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	context.title = _("Reference request")
	token = frappe.form_dict.get("t") or ""
	context.token = token
	try:
		context.view = references.referee_view(references.resolve_token(token, audit_failure=False))
	except InvalidAccessToken:
		context.view = None
	return context
