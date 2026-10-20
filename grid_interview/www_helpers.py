"""Shared context for the candidate portal pages (www/interview-portal/*)."""

import frappe
from frappe import _
from frappe.sessions import get_csrf_token
from frappe.utils import cint, format_datetime

from grid_interview.services import candidates
from grid_interview.utils.settings import get_settings

NAV = (
	("index", "/interview-portal", "Dashboard"),
	("profile", "/interview-portal/profile", "Profile & Documents"),
	("references", "/interview-portal/references", "References"),
	("account", "/interview-portal/account", "Account"),
)


def redirect(url: str):
	frappe.local.flags.redirect_location = url
	raise frappe.Redirect


def fmt(value, pattern="d MMM yyyy, HH:mm") -> str:
	return format_datetime(value, pattern) if value else ""


def candidate_page(context, active: str, path: str):
	"""Common setup for signed-in pages. Returns the portal profile or None (not a candidate)."""
	context.no_cache = 1
	context.show_sidebar = False
	settings = get_settings()
	context.portal_enabled = bool(cint(settings.enable_candidate_portal))
	context.nav = [{"key": k, "route": r, "label": _(label), "active": k == active} for k, r, label in NAV]
	context.support_email = settings.support_email
	context.support_phone = settings.support_phone
	if not context.portal_enabled:
		return None
	if frappe.session.user == "Guest":
		redirect(f"/login?redirect-to={path}")
	if not candidates.is_candidate():
		context.not_candidate = True
		return None
	# Frappe only injects an *existing* CSRF token into web pages and never creates one for
	# website users, which silently disables its CSRF check for them. Create it here so the
	# page carries frappe.csrf_token and every later POST is checked (by Frappe and by us).
	get_csrf_token()
	return candidates.require_candidate()


def guest_page(context):
	context.no_cache = 1
	context.show_sidebar = False
	settings = get_settings()
	context.portal_enabled = bool(cint(settings.enable_candidate_portal))
	context.allow_registration = context.portal_enabled and bool(cint(settings.allow_self_registration))
	context.support_email = settings.support_email
	if frappe.session.user != "Guest" and candidates.is_candidate():
		redirect("/interview-portal")
