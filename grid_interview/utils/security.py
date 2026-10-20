"""Token and fingerprint helpers. Raw tokens are never stored or logged, only their hashes."""

import hashlib
import hmac
import secrets

import frappe

TOKEN_BYTES = 32  # 256 bits of entropy


def new_token() -> tuple[str, str]:
	"""Return (raw_token, token_hash). Give the raw token to the user once; store only the hash."""
	raw = secrets.token_urlsafe(TOKEN_BYTES)
	return raw, hash_token(raw)


def hash_token(raw: str) -> str:
	return hashlib.sha256((raw or "").encode()).hexdigest()


def token_matches(raw: str, stored_hash: str) -> bool:
	if not raw or not stored_hash:
		return False
	return hmac.compare_digest(hash_token(raw), stored_hash)


def new_room_name() -> str:
	"""Unpredictable meeting room id (no interview names, applicant names or sequential ids)."""
	return "gi-" + secrets.token_hex(16)


def _pepper() -> bytes:
	return (frappe.local.conf.get("encryption_key") or frappe.local.site or "grid_interview").encode()


def client_fingerprint() -> str | None:
	"""Keyed hash of the client IP: lets us correlate abuse without storing raw IP addresses."""
	ip = getattr(frappe.local, "request_ip", None)
	if not ip:
		return None
	return hmac.new(_pepper(), ip.encode(), hashlib.sha256).hexdigest()[:32]
