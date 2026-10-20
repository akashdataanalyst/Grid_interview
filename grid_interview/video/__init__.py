"""Video provider abstraction.

The recruitment workflow only talks to `get_provider()`; switching the video engine means
adding a provider class, not changing scheduling, access control or attendance code.
"""

from grid_interview.video.base import Participant, VideoProvider


def get_provider(name: str | None = None) -> VideoProvider:
	from grid_interview.utils.settings import get_settings
	from grid_interview.video.jitsi import JitsiProvider
	from grid_interview.video.mock import MockProvider

	name = name or get_settings().video_provider or "Mock"
	providers = {"Mock": MockProvider, "Jitsi": JitsiProvider}
	if name not in providers:
		raise ValueError(f"Unknown video provider: {name}")
	return providers[name]()


__all__ = ["Participant", "VideoProvider", "get_provider"]
