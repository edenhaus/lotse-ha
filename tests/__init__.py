"""Tests for the lotse integration."""

import asyncio
from typing import override

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant

STREAM_SOURCE = "rtsp://admin:secret-password@192.0.2.10:554/h264"
"""A camera source with credentials, which must never be logged."""


class MockCamera(Camera):
    """A camera with a stream source."""

    _attr_supported_features: CameraEntityFeature = CameraEntityFeature.STREAM

    def __init__(self, unique_id: str, name: str = "Front door") -> None:
        """Initialize the camera."""
        super().__init__()
        self._stream_source: str | None = STREAM_SOURCE
        self._attr_unique_id = unique_id
        self._attr_name = name

    def set_stream_source(self, stream_source: str | None) -> None:
        """Change the stream source."""
        self._stream_source = stream_source

    @override
    async def stream_source(self) -> str | None:
        """Return the stream source."""
        return self._stream_source


async def run_tasks(hass: HomeAssistant) -> None:
    """Let background tasks, such as the session relays, take their turn."""
    for _ in range(5):
        await asyncio.sleep(0)
    await hass.async_block_till_done()
