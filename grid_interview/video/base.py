"""Provider contract: the Participant the server verified and the VideoProvider interface."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Participant:
	"""A server-verified participant. Built only by grid_interview.services.meeting."""

	kind: str  # "Candidate" | "Interviewer" | "HR"
	identity: str  # stable id: user name, or "access:<access session>" for link joins
	display_name: str
	email: str | None
	moderator: bool


class VideoProvider:
	name = "base"

	def join_config(self, session, participant: Participant, expires_at: datetime, recording_allowed: bool) -> dict:
		"""What the browser needs to open the embedded room. Must never contain secrets
		(only short-lived, room-scoped tokens)."""
		raise NotImplementedError
