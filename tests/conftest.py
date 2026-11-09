"""Fixtures for the lotse integration tests."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from lotse_client import LotseClient, Subscription
from lotse_client._commands import (
    MetricsCommands,
    SessionCommands,
    StreamCommands,
    WebrtcCommands,
)
from lotse_client.models import (
    Hello,
    InfoResult,
    Metrics,
    SessionEvent,
    StreamList,
    StreamPutResult,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
    setup_test_component_platform,
)

from custom_components.lotse.const import (
    CONF_BINARY,
    CONF_MODE,
    CONF_SOCKET,
    DOMAIN,
    MODE_CONNECT,
    MODE_SPAWN,
)
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import MockCamera

SOCKET_PATH = "/run/lotse/lotse.sock"
SPAWN_SOCKET_PATH = "/run/lotse-spawn/lotse.sock"
BINARY_PATH = "/usr/bin/lotse"

HELLO = Hello.from_dict(
    {
        "type": "hello",
        "api": "0.1.0",
        "version": "0.1.0",
        "outputs": ["webrtc"],
        "features": ["session_adopt", "turn"],
    }
)
INFO = InfoResult.from_dict(
    {
        "version": "0.1.0",
        "build": {"target": "x86_64-unknown-linux-musl", "rustc": "1.98.0"},
        # A build without the HTTP source: lotse's default build has it too.
        "protocols": ["rtsp", "rtsps", "webrtc"],
        "outputs": ["webrtc"],
        "features": ["session_adopt", "turn"],
        "codecs": {"video": ["h264", "h265"], "audio": ["opus", "pcmu"]},
        "limits": {
            "max_streams": 256,
            "max_sessions": 256,
            "max_sessions_per_stream": 16,
            "max_connections": 8,
            "session_grace_ms": 10000,
        },
        "sandbox": {
            "mode": "on",
            "uid": 65534,
            "gid": 65534,
            "no_new_privs": True,
            "seccomp": "enforced",
            "landlock": {"fs": "enforced", "net": "unavailable", "abi": 3},
        },
    }
)
METRICS = Metrics.from_dict(
    {
        "supervisor": {"pid": 4242, "uptime_ms": 60000},
        "workers": {},
        "worker_restarts": 0,
        "sessions": 1,
        "streams": {"camera.front_door": {"frames_dropped": 0}},
    }
)
STREAMS = StreamList.from_dict(
    {
        "streams": {
            "camera.front_door": {
                "stream_id": "camera.front_door",
                "state": "live",
                "preload": False,
                "since": "2026-11-06T10:00:00Z",
                "sessions": ["session-1"],
                "sources": [
                    {
                        "source": {
                            "protocol": "rtsp",
                            "url": "rtsp://****@192.0.2.10:554/h264",
                        },
                        "state": "live",
                        "reconnects": 0,
                        "connection": {"id": "c1"},
                    }
                ],
                "tracks": [],
                "stats": {},
            },
            "garage": {
                "stream_id": "garage",
                "state": "idle",
                "preload": True,
                "since": "2026-11-06T10:00:00Z",
                "sessions": [],
                "sources": [],
                "tracks": [],
                "stats": {},
            },
        }
    }
)


class MockClients:
    """The patched `LotseClient` class: a new mock client per connection."""

    def __init__(self) -> None:
        """Start with no client."""
        self.clients: list[MagicMock] = []
        self.paths: list[str] = []
        self.connect_errors: list[BaseException] = []
        """Raised by the next clients' `connect`, one each, in order."""
        self.configure: list[Callable[[MagicMock], object]] = []
        """Applied to the next clients, one each, in order."""

    def __call__(self, socket_path: str, **kwargs: Any) -> MagicMock:
        """Create the next client."""
        client = _mock_client()
        if self.connect_errors:
            client.connect.side_effect = self.connect_errors.pop(0)
        if self.configure:
            self.configure.pop(0)(client)
        self.clients.append(client)
        self.paths.append(socket_path)
        return client

    @property
    def last(self) -> MagicMock:
        """The newest client."""
        return self.clients[-1]


def _mock_client() -> MagicMock:
    """A connected client whose commands succeed."""
    client = MagicMock(spec=LotseClient)
    client.hello = HELLO
    client.connect = AsyncMock(return_value=HELLO)
    client.close = AsyncMock()
    client.info = AsyncMock(return_value=INFO)
    client.ping = AsyncMock()
    closed: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    client.closed = closed
    """Set its result to drop the connection."""

    async def _wait_closed() -> Any:
        return await asyncio.shield(closed)

    client.wait_closed = AsyncMock(side_effect=_wait_closed)
    client.unsubscribe_id = AsyncMock()
    client.subscriptions = []
    """Every session subscription opened or adopted, in order."""

    def _subscribe(**kwargs: Any) -> Subscription[SessionEvent]:
        subscription: Subscription[SessionEvent] = Subscription(client.unsubscribe_id)
        subscription.id = len(client.subscriptions) + 1
        subscription.result = None
        client.subscriptions.append(subscription)
        return subscription

    client.stream = MagicMock(spec=StreamCommands)
    client.stream.put.return_value = StreamPutResult(created=True)
    client.stream.delete.return_value = None
    client.stream.list.return_value = STREAMS
    client.webrtc = MagicMock(spec=WebrtcCommands)
    client.webrtc.offer.side_effect = _subscribe
    client.webrtc.candidate.return_value = None
    client.session = MagicMock(spec=SessionCommands)
    client.session.adopt.side_effect = _subscribe
    client.metrics = MagicMock(spec=MetricsCommands)
    client.metrics.get.return_value = METRICS
    return client


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture
def mock_clients() -> Generator[MockClients]:
    """Patch `LotseClient` wherever the integration creates one."""
    clients = MockClients()
    with (
        patch("custom_components.lotse.LotseClient", side_effect=clients),
        patch("custom_components.lotse.connection.LotseClient", side_effect=clients),
        patch("custom_components.lotse.server.LotseClient", side_effect=clients),
        patch("custom_components.lotse.config_flow.LotseClient", side_effect=clients),
    ):
        yield clients


@pytest.fixture
async def mock_server() -> AsyncGenerator[MagicMock]:
    """Patch the daemon Home Assistant runs."""
    with patch("custom_components.lotse.LotseServer", autospec=True) as server_class:
        server = server_class.return_value
        loop = asyncio.get_running_loop()
        exited: list[asyncio.Future[int]] = [loop.create_future()]

        def _exit(code: int) -> None:
            """Make the running daemon exit with `code`; a restarted one runs on."""
            future = exited[0]
            exited[0] = loop.create_future()
            future.set_result(code)

        server.exit = _exit

        async def _wait() -> int:
            return await asyncio.shield(exited[0])

        server.async_wait.side_effect = _wait
        server.async_ensure_running.return_value = SPAWN_SOCKET_PATH
        server.pid = 4242
        yield server


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Patch setting up an entry."""
    with patch(
        "custom_components.lotse.async_setup_entry", return_value=True
    ) as setup_entry:
        yield setup_entry


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """An entry that connects to a running daemon."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="lotse",
        data={CONF_MODE: MODE_CONNECT, CONF_SOCKET: SOCKET_PATH},
    )


@pytest.fixture
def mock_spawn_entry() -> MockConfigEntry:
    """An entry that runs the daemon."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="lotse",
        data={CONF_MODE: MODE_SPAWN, CONF_BINARY: BINARY_PATH},
    )


@pytest.fixture
async def init_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_clients: MockClients
) -> MockConfigEntry:
    """Set up the integration, connected to a mocked daemon."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


TEST_DOMAIN = "test"


async def async_setup_camera(hass: HomeAssistant, camera: MockCamera) -> None:
    """Set up a camera of another integration."""

    async def async_setup_entry_init(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.CAMERA])
        return True

    async def async_unload_entry_init(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        return await hass.config_entries.async_forward_entry_unload(
            entry, Platform.CAMERA
        )

    mock_integration(
        hass,
        MockModule(
            TEST_DOMAIN,
            async_setup_entry=async_setup_entry_init,
            async_unload_entry=async_unload_entry_init,
        ),
    )
    setup_test_component_platform(hass, CAMERA_DOMAIN, [camera], from_config_entry=True)
    mock_platform(hass, f"{TEST_DOMAIN}.config_flow", Mock())
    entry = MockConfigEntry(domain=TEST_DOMAIN)
    entry.add_to_hass(hass)
    with mock_config_flow(TEST_DOMAIN, ConfigFlow):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def camera(hass: HomeAssistant, init_integration: MockConfigEntry) -> MockCamera:
    """A camera of another integration, set up after lotse."""
    camera = MockCamera("camera_unique_id")
    await async_setup_camera(hass, camera)
    return camera
