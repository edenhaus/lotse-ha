"""Constants for the lotse integration."""

from typing import Final

DOMAIN: Final = "lotse"

CONF_MODE: Final = "mode"
CONF_BINARY: Final = "binary"
CONF_SOCKET: Final = "socket"

MODE_SPAWN: Final = "spawn"
"""Home Assistant runs the daemon itself, from the binary in `CONF_BINARY`."""
MODE_CONNECT: Final = "connect"
"""Home Assistant connects to a daemon at the control socket in `CONF_SOCKET`."""

BINARY_NAME: Final = "lotse"
DEFAULT_SOCKET: Final = "/run/lotse/lotse.sock"
SOCKET_NAME: Final = "lotse.sock"

PING_INTERVAL: Final = 10
"""Seconds between two pings on the control connection."""
PING_TIMEOUT: Final = 5
"""Seconds a pong may take before the daemon counts as unresponsive."""
RECONNECT_MAX_DELAY: Final = 60
"""The longest wait, in seconds, between two reconnect attempts."""

ERROR_UNAVAILABLE: Final = "lotse_unavailable"
"""The code of the WebRTC error sent while the daemon cannot be reached."""
ERROR_NO_STREAM_SOURCE: Final = "no_stream_source"
"""The code of the WebRTC error sent for a camera without a stream source."""
