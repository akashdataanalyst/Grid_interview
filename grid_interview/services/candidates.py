"""Candidate accounts: standard Frappe Website Users with the Grid Interview Candidate role.

No separate password store: passwords are set through Frappe's own welcome / reset-password
links (/update-password), and login is Frappe's /login. A candidate sees the Job Applicants
whose email equals their account email; owning the email is proven by having set the password
through a link sent to it.
"""

import hmac
import re

import frappe
from frappe import _
from frappe.utils import cint, now_datetime, validate_email_address

from grid_interview.setup.install import CANDIDATE_ROLE
from grid_interview.utils import audit
from grid_interview.utils.settings import get_settings

PROFILE = "Grid Interview Portal Profile"
_NAME_RE = re.compile(r"^[^<>\"`{}]{2,140}$")


def normalize_email(email: str) -> str:
	email = (email or "").strip().lower()
	if not email or not validate_email_address(email):
		frappe.throw(_("Enter a valid email address."))
	return email


def is_candidate(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return user not in ("Guest", None) and CANDIDATE_ROLE in frappe.get_roles(user)


def ensure_candidate_account(email: str, full_name: str, source: str, send_welcome_email=0) -> str | None:
	"""Create (or reuse) the candidate's Website User and portal profile.

	Existing System Users (employees, administrators) are never given the candidate role
	automatically; None is returned for them and an audit row explains why.
	"""
	email = normalize_email(email)
	user = frappe.db.get_value("User", {"email": email}, ["name", "user_type", "enabled"], as_dict=True)
	if user:
		if user.user_type != "Website User":
			audit.log("candidate.account_skipped", "User", user.name, reason="existing system user")
			return None
		if not user.enabled:
			return None
		if CANDIDATE_ROLE not in frappe.get_roles(user.name):
			user_doc = frappe.get_doc("User", user.name)
			user_doc.flags.ignore_permissions = True  # adding only our desk-less role to a website user
			user_doc.add_roles(CANDIDATE_ROLE)
		ensure_profile(user.name, full_name, email, source)
		return user.name

	first, _sep, last = (full_name or email.split("@")[0]).strip().partition(" ")
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first[:140],
			"last_name": last[:140] or None,
			"user_type": "Website User",
			"send_welcome_email": cint(send_welcome_email),
			"roles": [{"role": CANDIDATE_ROLE}],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	ensure_profile(doc.name, full_name, email, source)
	audit.log("candidate.account_created", "User", doc.name, actor="Administrator", source=source)
	return doc.name


def ensure_profile(user: str, full_name: str, email: str, source: str) -> str:
	if frappe.db.exists(PROFILE, user):
		return user
	frappe.get_doc(
		{
			"doctype": PROFILE,
			"user": user,
			"full_name": (full_name or "").strip()[:140] or user,
			"email": email,
			"status": "Active",
			"registration_source": source,
		}
	).insert(ignore_permissions=True)
	return user


UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def check_csrf():
	"""Explicit CSRF check for state-changing candidate requests.

	Frappe skips its own check when the session has no CSRF token, which is the default for
	website users. Here a missing token is a failure, not a pass."""
	request = getattr(frappe.local, "request", None)
	if not request or request.method not in UNSAFE_METHODS:
		return
	saved = (frappe.session.data or {}).get("csrf_token")
	sent = frappe.get_request_header("X-Frappe-CSRF-Token")
	if not saved or not sent or not hmac.compare_digest(str(sent), str(saved)):
		audit.log("candidate.csrf_rejected", result="Denied")
		raise frappe.CSRFTokenError(_("Your session has expired. Please reload the page and try again."))


def require_candidate() -> frappe._dict:
	"""Current user's active portal profile, or PermissionError. Every portal API starts here."""
	user = frappe.session.user
	if not is_candidate(user):
		raise frappe.PermissionError(_("Please sign in with your candidate account."))
	check_csrf()
	if not frappe.db.exists(PROFILE, user):
		ensure_profile(user, frappe.utils.get_fullname(user), frappe.db.get_value("User", user, "email"), "HR Created")
	profile = frappe.db.get_value(PROFILE, user, ["name", "user", "email", "full_name", "status"], as_dict=True)
	if profile.status != "Active":
		raise frappe.PermissionError(_("This candidate account is disabled."))
	return profile


def register(full_name: str, email: str, accept_privacy_notice=0) -> str:
	"""Self-registration. The response never reveals whether the email already has an account."""
	generic = _("If this email can be registered, you will receive an email with a link to set your password.")
	settings = get_settings()
	if not cint(settings.enable_candidate_portal) or not cint(settings.allow_self_registration):
		frappe.throw(_("Self-registration is not open. Please use the link in your interview email."))
	if not cint(accept_privacy_notice):
		frappe.throw(_("Please accept the privacy notice to continue."))
	full_name = (full_name or "").strip()
	if not _NAME_RE.match(full_name):
		frappe.throw(_("Enter your full name."))
	email = normalize_email(email)
	if frappe.db.exists("User", {"email": email}):
		audit.log("candidate.register_existing", actor="Guest")
		return generic
	user = ensure_candidate_account(email, full_name, "Self Registration", send_welcome_email=1)
	if user:
		frappe.db.set_value(
			PROFILE,
			user,
			{"privacy_notice_version": settings.privacy_notice_version, "privacy_notice_accepted_on": now_datetime()},
		)
	return generic
