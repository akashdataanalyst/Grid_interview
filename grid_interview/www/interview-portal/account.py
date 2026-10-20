from frappe import _

from grid_interview.services import portal
from grid_interview.www_helpers import candidate_page, fmt

no_cache = 1
no_sitemap = 1


def get_context(context):
	context.title = _("Account")
	profile = candidate_page(context, "account", "/interview-portal/account")
	if not profile:
		return context
	history = portal.notification_history(profile)
	for h in history:
		h["when"] = fmt(h["creation"], "d MMM yyyy, HH:mm")
	context.history = history
	context.email = profile.email
	context.loaded = True
	return context
