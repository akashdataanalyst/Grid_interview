from frappe import _
from frappe.utils import cint

from grid_interview.services import portal
from grid_interview.utils.settings import get_settings
from grid_interview.www_helpers import candidate_page, fmt

no_cache = 1
no_sitemap = 1


def get_context(context):
	context.title = _("Profile & Documents")
	profile = candidate_page(context, "profile", "/interview-portal/profile")
	if not profile:
		return context
	settings = get_settings()
	context.profile = portal.get_profile(profile)
	docs = portal.list_documents(profile)
	for d in docs:
		d["uploaded"] = fmt(d["creation"], "d MMM yyyy")
		d["size_kb"] = max(1, cint(d["file_size"]) // 1024)
	context.documents = docs
	context.allowed = ", ".join("." + e for e in (settings.allowed_upload_extensions or "").split(",") if e)
	context.accept = ",".join("." + e for e in (settings.allowed_upload_extensions or "").split(",") if e)
	context.max_mb = settings.max_upload_size_mb
	return context
