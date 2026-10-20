from frappe import _

from grid_interview.utils.settings import get_settings
from grid_interview.www_helpers import guest_page

no_cache = 1
no_sitemap = 1


def get_context(context):
	context.title = _("Create candidate account")
	guest_page(context)
	settings = get_settings()
	context.privacy_notice = settings.privacy_notice
	return context
