"""Install / migrate hooks. Idempotent: only creates grid_interview's own roles.

Nothing here touches standard DocTypes, Custom Fields, Property Setters or other apps' records.
"""

import frappe

ADMIN_ROLE = "Grid Interview Administrator"
HR_ROLE = "Grid Interview HR User"
REVIEWER_ROLE = "Grid Interview Reviewer"
CANDIDATE_ROLE = "Grid Interview Candidate"

# (role, desk_access). Candidates are Website Users and must never get Desk access.
ROLES = (
	(ADMIN_ROLE, 1),
	(HR_ROLE, 1),
	(REVIEWER_ROLE, 1),
	(CANDIDATE_ROLE, 0),
)


def after_install():
	create_roles()


def after_migrate():
	create_roles()


def create_roles():
	for role_name, desk_access in ROLES:
		if frappe.db.exists("Role", role_name):
			# keep desk_access correct for the candidate role even if someone changed it
			if not desk_access and frappe.db.get_value("Role", role_name, "desk_access"):
				frappe.db.set_value("Role", role_name, "desk_access", 0)
			continue
		frappe.get_doc(
			{"doctype": "Role", "role_name": role_name, "desk_access": desk_access, "is_custom": 0}
		).insert(ignore_permissions=True)
