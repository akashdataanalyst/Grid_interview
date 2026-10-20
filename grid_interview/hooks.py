app_name = "grid_interview"
app_title = "Grid Interview"
app_publisher = "Calco PolyTechnik Pvt Ltd"
app_description = "Recruitment interview platform: scheduling, candidate portal, embedded video, consent-based recording, transcription and human-reviewed AI reports"
app_email = "mis.1@calco.in"
app_license = "mit"

# HRMS is optional: all references to HRMS records are Dynamic Links resolved through
# grid_interview.integrations.hrms_adapter, so the app installs (with reduced features) without it.
required_apps = ["frappe"]

app_include_js = ["/assets/grid_interview/js/grid_interview.js"]

# Candidates (website users with our role) land on the portal after login. Not role_home_page:
# Frappe v16 applies that to Desk users too, and Administrator has every role.
get_website_user_home_page = "grid_interview.permissions.website_user_home_page"

portal_menu_items = [
	{"title": "My Interviews", "route": "/interview-portal", "role": "Grid Interview Candidate"},
]

after_install = "grid_interview.setup.install.after_install"
after_migrate = "grid_interview.setup.install.after_migrate"

# Interviewers only see app records that belong to interviews they are assigned to.
permission_query_conditions = {
	"Grid Interview Video Session": "grid_interview.permissions.video_session_query",
	"Grid Interview Transcript": "grid_interview.permissions.interview_linked_query",
	"Grid Interview Analysis": "grid_interview.permissions.interview_linked_query",
}

scheduler_events = {
	"cron": {
		# reconcile Desk changes, send due reminders, sync Email Queue status / retries
		"*/5 * * * *": ["grid_interview.services.scheduling.run_cycle"],
	},
	"daily": ["grid_interview.services.retention.run"],
}

add_to_apps_screen = [
	{
		"name": "grid_interview",
		"logo": "/assets/grid_interview/images/grid_interview.svg",
		"title": "Interviews",
		"route": "/desk/grid-interview-dashboard",
		"has_permission": "grid_interview.permissions.can_open_app",
	}
]

has_permission = {
	"Grid Interview Video Session": "grid_interview.permissions.has_interview_linked_permission",
	"Grid Interview Transcript": "grid_interview.permissions.has_interview_linked_permission",
	"Grid Interview Analysis": "grid_interview.permissions.has_interview_linked_permission",
}
