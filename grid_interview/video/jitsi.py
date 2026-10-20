"""Self-hosted Jitsi Meet via the official IFrame API (https://<domain>/external_api.js).

Access control is token-based (Jitsi "JWT authentication", prosody token auth):
the server must run with ENABLE_AUTH=1, AUTH_TYPE=jwt, JWT_APP_ID / JWT_APP_SECRET matching
Grid Interview Settings, and anonymous / guest access disabled. Each participant gets an HS256
token scoped to one room, with a short expiry and the moderator flag decided here, on the server.
The app secret never leaves the server.
"""

import time

import jwt

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from grid_interview.utils.settings import get_secret, get_settings
from grid_interview.video.base import Participant, VideoProvider


class JitsiProvider(VideoProvider):
	name = "Jitsi"

	def token(self, session, participant: Participant, expires_at, recording_allowed: bool) -> str:
		settings = get_settings()
		secret = get_secret("jitsi_app_secret")
		if not (settings.jitsi_domain and settings.jitsi_app_id and secret):
			frappe.throw(_("The video service is not configured. Please contact HR."))
		now = int(time.time())
		ttl = cint(settings.jitsi_token_ttl_minutes or 120) * 60
		seconds_to_close = int((expires_at - now_datetime()).total_seconds())
		exp = now + max(60, min(ttl, seconds_to_close))
		claims = {
			"aud": settings.jitsi_jwt_audience or "jitsi",
			"iss": settings.jitsi_app_id,
			"sub": settings.jitsi_domain,
			"room": session.room_name,  # one room only, never "*"
			"nbf": now - 10,
			"iat": now,
			"exp": exp,
			"context": {
				"user": {
					"id": participant.identity,
					"name": participant.display_name,
					"email": participant.email or "",
					"moderator": participant.moderator,
				},
				"features": {
					"recording": bool(recording_allowed and participant.moderator),
					"livestreaming": False,
					"transcription": False,
					"outbound-call": False,
					"sip-outbound-call": False,
				},
			},
		}
		return jwt.encode(claims, secret, algorithm="HS256")

	def join_config(self, session, participant: Participant, expires_at, recording_allowed: bool) -> dict:
		settings = get_settings()
		return {
			"provider": self.name,
			"domain": settings.jitsi_domain,
			"external_api": f"https://{settings.jitsi_domain}/external_api.js",
			"room": session.room_name,
			"jwt": self.token(session, participant, expires_at, recording_allowed),
			"display_name": participant.display_name,
			"moderator": participant.moderator,
			"enable_lobby": bool(cint(settings.enable_lobby)),
			"expires_at": expires_at.isoformat(),
			"recording_allowed": bool(recording_allowed),
			"test_mode": False,
		}
