"""Tests for setting up and unloading the lotse integration."""

from unittest.mock import MagicMock

from lotse_client import ApiVersionError, CannotConnectError, ConnectionClosedError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lotse import LotseData
from custom_components.lotse.provider import LotseWebRTCProvider
from custom_components.lotse.server import LotseServerError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from .conftest import SOCKET_PATH, SPAWN_SOCKET_PATH, MockClients


async def test_setup_and_unload(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_clients: MockClients
) -> None:
    """The entry connects to the socket, and unloading closes the connection."""
    entry = init_integration
    assert entry.state is ConfigEntryState.LOADED
    assert mock_clients.paths == [SOCKET_PATH]
    client = mock_clients.last
    client.connect.assert_awaited_once()
    client.info.assert_awaited_once()
    assert isinstance(entry.runtime_data, LotseData)
    assert isinstance(entry.runtime_data.provider, LotseWebRTCProvider)
    connection = entry.runtime_data.connection
    assert connection.client is client
    assert connection.server is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    client.close.assert_awaited_once()
    assert connection.client is None


async def test_setup_cannot_connect(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_clients: MockClients
) -> None:
    """A daemon that cannot be reached makes setup retry."""
    mock_clients.connect_errors = [CannotConnectError("refused")]
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert mock_config_entry.reason == "Could not connect to lotse: refused"


async def test_setup_info_fails(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_clients: MockClients
) -> None:
    """A connection that drops before `info` is answered is closed and retried."""
    mock_clients.configure = [
        lambda client: setattr(
            client.info, "side_effect", ConnectionClosedError("closed")
        )
    ]
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_clients.last.close.assert_awaited_once()


async def test_setup_api_version(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_clients: MockClients
) -> None:
    """A daemon of another API version fails the setup for good."""
    mock_clients.connect_errors = [ApiVersionError("0.2.0", "0.1.0")]
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    assert mock_config_entry.reason == (
        "lotse speaks control API version 0.2.0, but this integration needs a "
        "version compatible with 0.1.0"
    )


async def test_stop_with_home_assistant(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_clients: MockClients
) -> None:
    """Home Assistant stopping closes the connection."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    mock_clients.last.close.assert_awaited_once()
    assert init_integration.runtime_data.connection.client is None


async def test_spawn_setup_and_unload(
    hass: HomeAssistant,
    mock_spawn_entry: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
) -> None:
    """In spawn mode the daemon is started first and stopped on unload."""
    mock_spawn_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_spawn_entry.entry_id)
    assert mock_spawn_entry.state is ConfigEntryState.LOADED
    mock_server.async_ensure_running.assert_awaited_once_with(restart=False)
    assert mock_clients.paths == [SPAWN_SOCKET_PATH]
    assert mock_spawn_entry.runtime_data.connection.server is mock_server

    assert await hass.config_entries.async_unload(mock_spawn_entry.entry_id)
    mock_clients.last.close.assert_awaited_once()
    mock_server.async_stop.assert_awaited_once()


async def test_spawn_setup_fails(
    hass: HomeAssistant,
    mock_spawn_entry: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
) -> None:
    """A daemon that does not start makes setup retry, and is stopped."""
    mock_server.async_ensure_running.side_effect = LotseServerError("exited")
    mock_spawn_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_spawn_entry.entry_id)
    assert mock_spawn_entry.state is ConfigEntryState.SETUP_RETRY
    mock_server.async_stop.assert_awaited_once()
    assert not mock_clients.clients


@pytest.mark.usefixtures("mock_clients")
async def test_spawn_api_version(
    hass: HomeAssistant,
    mock_spawn_entry: MockConfigEntry,
    mock_server: MagicMock,
) -> None:
    """A spawned daemon of another API version fails the setup and is stopped."""
    mock_server.async_ensure_running.side_effect = ApiVersionError("0.2.0", "0.1.0")
    mock_spawn_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_spawn_entry.entry_id)
    assert mock_spawn_entry.state is ConfigEntryState.SETUP_ERROR
    mock_server.async_stop.assert_awaited_once()
