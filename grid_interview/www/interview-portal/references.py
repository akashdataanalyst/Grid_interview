from frappe import _
from frappe.utils import cint

from grid_interview.services import portal, references
from grid_interview.utils.settings import get_settings
from grid_interview.www_helpers import candidate_page

no_cache = 1
no_sitemap = 1

STATUS_PILL = {
	"Submitted": "gray", "Clarification Requested": "orange", "Verification Sent": "blue",
	"Verified": "green", "Unable to Verify": "gray", "Declined": "gray",
}


def get_context(context):
	context.title = _("References")
	profile = candidate_page(context, "references", "/interview-portal/references")
	if not profile:
		return context
	refs = references.list_references(profile)
	for r in refs:
		r["pill"] = STATUS_PILL.get(r["verification_status"], "gray")
		r["editable"] = not r["locked"] and r["verification_status"] in references.CANDIDATE_EDITABLE_STATUSES
	context.references = refs
	context.applications = portal.get_dashboard(profile)["applications"]
	context.can_add = len(refs) < cint(get_settings().max_references or 5)
	context.loaded = True
	return context
