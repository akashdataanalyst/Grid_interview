"""Interview room endpoints.

Signed-in participants are identified by their Frappe session; candidates using a
passwordless join link are identified by the HttpOnly, browser-bound `gi_access` cookie.
All decisions (who, when, moderator or not) are made in grid_interview.services.meeting.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from grid_interview.exceptions import AccessDenied
from grid_interview.services import meeting


def _access(video_session: str):
	"""Link-join access for Guests; None for signed-in users (resolved from the session)."""
	if frappe.session.user != "Guest":
		return None
	access = meeting.access_from_cookie(video_session)
	if not access:
		raise AccessDenied(_("Please open your interview link or sign in to join."))
	return access


@frappe.whitelist(allow_guest=True, methods=["GET"])
def room_info(video_session: str):
	return meeting.room_details(video_session, _access(video_session))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60 * 60)
def join(video_session: str):
	return meeting.join(video_session, _access(video_session))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=60 * 60)
def heartbeat(video_session: str):
	return meeting.heartbeat(video_session, _access(video_session))


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=120, seconds=60 * 60)
def leave(video_session: str):
	return meeting.leave(video_session, _access(video_session))


@frappe.whitelist(methods=["POST"])
def end_interview(video_session: str):
	return meeting.end_meeting(video_session)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def redeem_link(token: str):
	"""Called by the join page's button (POST, so link scanners that only GET cannot burn the
	link). Binds the link to this browser with an HttpOnly cookie."""
	if frappe.get_request_header("X-Grid-Interview") != "1":
		raise frappe.CSRFTokenError(_("Invalid request."))
	cookie = frappe.request.cookies.get(meeting.COOKIE) if frappe.request else None
	access, secret = meeting.redeem_link(token, cookie)
	if secret:
		meeting.set_access_cookie(access, secret)
	return {"video_session": access.video_session}


@frappe.whitelist(methods=["POST"])
def issue_join_link(video_session: str):
	meeting.issue_join_link(video_session)
	return {"ok": True}
