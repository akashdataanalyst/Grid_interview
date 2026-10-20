"""Mock provider for development and automated tests.

The browser shows a local camera/microphone preview and reports join / leave / heartbeat
events exactly like a real room, so access control and attendance can be tested end to end.
No media is transmitted between participants. It must not be used for real interviews.
"""

from grid_interview.video.base import Participant, VideoProvider


class MockProvider(VideoProvider):
	name = "Mock"

	def join_config(self, session, participant: Participant, expires_at, recording_allowed: bool) -> dict:
		return {
			"provider": self.name,
			"room": session.room_name,
			"display_name": participant.display_name,
			"moderator": participant.moderator,
			"expires_at": expires_at.isoformat(),
			"recording_allowed": bool(recording_allowed),
			"test_mode": True,
		}
