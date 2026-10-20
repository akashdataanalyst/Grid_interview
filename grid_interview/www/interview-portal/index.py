from frappe import _

from grid_interview.services import portal
from grid_interview.www_helpers import candidate_page, fmt

no_cache = 1
no_sitemap = 1

PILL = {
	"Scheduled": "blue", "In Progress": "orange", "Completed": "green", "Cancelled": "gray",
	"Expired": "gray", "No Show": "gray",
	"Applied": "gray", "Under Review": "blue", "Interview Scheduled": "blue", "Interview In Progress": "orange",
	"Interview Completed": "blue", "Next Round": "green", "On Hold": "orange", "Selected": "green", "Not Selected": "gray",
}


def get_context(context):
	context.title = _("My Interviews")
	profile = candidate_page(context, "index", "/interview-portal")
	if not profile:
		return context
	data = portal.get_dashboard(profile)
	for card in data["upcoming"] + data["past"]:
		card["when"] = fmt(card["start"], "EEEE, d MMM yyyy")
		card["time"] = f"{fmt(card['start'], 'HH:mm')} – {fmt(card['end'], 'HH:mm')}"
		card["opens"] = fmt(card["join_opens_at"], "d MMM, HH:mm")
		card["pill"] = PILL.get(card["status"], "gray")
	for app in data["applications"]:
		app["pill"] = PILL.get(app["status"], "gray")
	for res in data["results"]:
		res["published_label"] = fmt(res["published_on"], "d MMM yyyy")
		res["pill"] = PILL.get(res["candidate_status"], "gray")
	context.data = data
	return context
