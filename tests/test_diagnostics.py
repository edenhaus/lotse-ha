"""Tests for the lotse diagnostics."""

from unittest.mock import MagicMock

from lotse_client import CannotConnectError, ConnectionClosedError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from syrupy.assertion import SnapshotAssertion

from homeassistant.core import HomeAssistant

from . import run_tasks
from .conftest import MockClients


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    snapshot: SnapshotAssertion,
) -> None:
    """Diagnostics carry the daemon's state, without addresses or credentials."""
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result == snapshot
    assert "192.0.2.10" not in str(result)


async def test_diagnostics_spawned(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_spawn_entry: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
) -> None:
    """Diagnostics name the spawned daemon's process."""
    mock_spawn_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_spawn_entry.entry_id)
    result = await get_diagnostics_for_config_entry(hass, hass_client, mock_spawn_entry)
    assert result["server_pid"] == 4242
    assert result["entry_data"] == {"mode": "spawn", "binary": "/usr/bin/lotse"}


async def test_diagnostics_disconnected(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
) -> None:
    """While the daemon cannot be reached, the last `info` is reported."""
    mock_clients.connect_errors = [CannotConnectError("refused")]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result["connected"] is False
    assert result["info"]["version"] == "0.1.0"
    assert "metrics" not in result


async def test_diagnostics_command_fails(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
) -> None:
    """A command that fails while collecting is reported."""
    mock_clients.last.metrics.get.side_effect = ConnectionClosedError("closed")
    result = await get_diagnostics_for_config_entry(hass, hass_client, init_integration)
    assert result["error"] == "closed"
    assert "metrics" not in result
