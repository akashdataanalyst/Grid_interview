"""HR actions on candidate-facing records. Authorization is enforced in the services."""

import frappe

from grid_interview.services import references, results


@frappe.whitelist(methods=["POST"])
def publish_result(name: str):
	results.publish(name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def unpublish_result(name: str):
	results.unpublish(name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def send_reference_verification(name: str):
	references.send_verification(name)
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def request_reference_clarification(name: str, message: str):
	references.request_clarification(name, message)
	return {"ok": True}
