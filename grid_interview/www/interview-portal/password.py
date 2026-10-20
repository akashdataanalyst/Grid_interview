from frappe import _

from grid_interview.www_helpers import guest_page

no_cache = 1
no_sitemap = 1


def get_context(context):
	context.title = _("Set or reset password")
	guest_page(context)
	return context
