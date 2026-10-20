"""Grid Interview Settings (single DocType) helpers: cached settings, secrets, parsed values."""

import frappe

SETTINGS = "Grid Interview Settings"


def get_settings():
	return frappe.get_cached_doc(SETTINGS)


def get_secret(fieldname: str) -> str | None:
	"""Read a Password field server-side. Never return the result to the browser."""
	return get_settings().get_password(fieldname, raise_exception=False) or None


def reminder_offsets(value: str | None = None) -> list[int]:
	"""Parse '1440,60,15' into [1440, 60, 15] (unique, positive, descending)."""
	raw = get_settings().reminder_offsets_minutes if value is None else value
	offsets = set()
	for part in (raw or "").split(","):
		part = part.strip()
		if not part:
			continue
		if not part.isdigit() or int(part) <= 0:
			raise frappe.ValidationError(f"Invalid reminder offset: {part!r}")
		offsets.add(int(part))
	return sorted(offsets, reverse=True)
