"""Tests for supervising the connection to lotse."""

import asyncio
from datetime import timedelta
import logging
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from lotse_client import CannotConnectError, ConnectionClosedError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.lotse.connection import LotseConnection, reconnect_delay
from custom_components.lotse.server import LotseServerError
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from . import run_tasks
from .conftest import MockClients


@pytest.fixture
async def spawned(
    hass: HomeAssistant,
    mock_spawn_entry: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
) -> MockConfigEntry:
    """An entry that runs the daemon, set up."""
    mock_spawn_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_spawn_entry.entry_id)
    await hass.async_block_till_done()
    return mock_spawn_entry


def _connection(entry: MockConfigEntry) -> LotseConnection:
    return entry.runtime_data.connection


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await run_tasks(hass)


@pytest.mark.parametrize(
    ("attempt", "delay"),
    [(0, 0), (1, 1), (2, 2), (3, 4), (6, 32), (7, 60), (1000, 60)],
)
def test_reconnect_delay(attempt: int, delay: int) -> None:
    """Attempts double their delay from 1 s up to a minute; the first is immediate."""
    assert reconnect_delay(attempt) == delay


async def test_ping(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The connection is pinged every 10 s."""
    client = mock_clients.last
    client.ping.assert_not_awaited()
    await _tick(hass, freezer, 10)
    client.ping.assert_awaited_once()
    await _tick(hass, freezer, 10)
    assert client.ping.await_count == 2
    assert _connection(init_integration).client is client


async def test_ping_timeout_reconnects(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ping without pong drops the connection and opens a new one."""
    caplog.set_level(logging.INFO)
    old = mock_clients.last
    old.ping.side_effect = TimeoutError
    await _tick(hass, freezer, 10)

    assert "Lost the connection to lotse: no pong within 5 s" in caplog.text
    old.close.assert_awaited_once()
    assert len(mock_clients.clients) == 2
    assert _connection(init_integration).client is mock_clients.last
    assert "Reconnected to lotse 0.1.0" in caplog.text

    # The old connection's ping timer is gone, the new one pings.
    await _tick(hass, freezer, 10)
    old.ping.assert_awaited_once()
    mock_clients.last.ping.assert_awaited_once()


async def test_ping_fails_reconnects(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ping that fails on a closed connection reconnects."""
    mock_clients.last.ping.side_effect = ConnectionClosedError("not connected")
    await _tick(hass, freezer, 10)
    assert "Lost the connection to lotse: ping failed: not connected" in caplog.text
    assert len(mock_clients.clients) == 2


async def test_connection_closed_reconnects(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A connection the daemon closes is reopened at once."""
    mock_clients.last.closed.set_result(ConnectionClosedError("the daemon shut down"))
    await run_tasks(hass)
    assert (
        "Lost the connection to lotse: connection closed: the daemon shut down"
        in caplog.text
    )
    assert len(mock_clients.clients) == 2
    assert _connection(init_integration).client is mock_clients.last


async def test_late_failure_of_old_connection_ignored(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ping that fails after its connection was replaced changes nothing."""
    old = mock_clients.last
    release = asyncio.Event()

    async def _slow_ping() -> None:
        await release.wait()
        raise TimeoutError

    old.ping.side_effect = _slow_ping
    await _tick(hass, freezer, 10)
    old.closed.set_result(None)
    await run_tasks(hass)
    assert len(mock_clients.clients) == 2

    caplog.clear()
    release.set()
    await run_tasks(hass)
    assert "Lost the connection" not in caplog.text
    assert _connection(init_integration).client is mock_clients.last


async def test_reconnect_backoff(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failed reconnects are retried after growing delays."""
    caplog.set_level(logging.INFO)
    mock_clients.connect_errors = [CannotConnectError("refused")] * 3
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    assert len(mock_clients.clients) == 2
    assert _connection(init_integration).client is None
    assert "Reconnecting to lotse in 1 s" in caplog.text

    await _tick(hass, freezer, 1)
    assert len(mock_clients.clients) == 3
    assert "Reconnecting to lotse in 2 s" in caplog.text

    await _tick(hass, freezer, 1)
    assert len(mock_clients.clients) == 3
    await _tick(hass, freezer, 1)
    assert len(mock_clients.clients) == 4
    assert "Reconnecting to lotse in 4 s" in caplog.text

    await _tick(hass, freezer, 4)
    assert len(mock_clients.clients) == 5
    assert _connection(init_integration).client is mock_clients.last

    # A success starts the delays over.
    caplog.clear()
    mock_clients.connect_errors = [CannotConnectError("refused")]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    assert "Reconnecting to lotse in 1 s" in caplog.text


async def test_unload_while_waiting_to_reconnect(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unloading cancels the next reconnect attempt."""
    mock_clients.connect_errors = [CannotConnectError("refused")]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    assert len(mock_clients.clients) == 2

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await _tick(hass, freezer, 60)
    assert len(mock_clients.clients) == 2


async def test_stopped_while_reconnecting(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_clients: MockClients
) -> None:
    """A connection opened after the stop is closed again."""
    connection = _connection(init_integration)

    async def _connect_while_stopping() -> None:
        await connection.async_stop()

    mock_clients.configure = [
        lambda client: setattr(client.connect, "side_effect", _connect_while_stopping)
    ]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    mock_clients.last.close.assert_awaited_once()
    assert connection.client is None


async def test_failed_while_stopping(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reconnect that fails after the stop schedules no other."""
    connection = _connection(init_integration)

    async def _fail_while_stopping() -> None:
        await connection.async_stop()
        raise CannotConnectError("refused")

    mock_clients.configure = [
        lambda client: setattr(client.connect, "side_effect", _fail_while_stopping)
    ]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    await _tick(hass, freezer, 60)
    assert len(mock_clients.clients) == 2


async def test_lost_after_stop_ignored(
    hass: HomeAssistant, init_integration: MockConfigEntry, mock_clients: MockClients
) -> None:
    """Home Assistant stopping ends the supervision."""
    client = mock_clients.last
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    client.closed.set_result(None)
    await run_tasks(hass)
    assert len(mock_clients.clients) == 1


async def test_spawned_exit_restarts(
    hass: HomeAssistant,
    spawned: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawned daemon that exits is restarted."""
    caplog.set_level(logging.INFO)
    mock_server.exit(1)
    await run_tasks(hass)
    assert "Lost the connection to lotse: lotse exited with code 1" in caplog.text
    assert "Restarting lotse" in caplog.text
    mock_server.async_ensure_running.assert_awaited_with(restart=True)
    assert len(mock_clients.clients) == 2
    assert _connection(spawned).client is mock_clients.last


async def test_spawned_connection_closed_reconnects(
    hass: HomeAssistant,
    spawned: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A closed connection to a spawned daemon is reopened before it is restarted."""
    mock_clients.connect_errors = [CannotConnectError("refused")]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    mock_server.async_ensure_running.assert_awaited_with(restart=False)

    await _tick(hass, freezer, 1)
    mock_server.async_ensure_running.assert_awaited_with(restart=True)
    assert _connection(spawned).client is mock_clients.last


async def test_spawned_ping_timeout_restarts(
    hass: HomeAssistant,
    spawned: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A spawned daemon that does not answer is restarted."""
    mock_clients.last.ping.side_effect = TimeoutError
    await _tick(hass, freezer, 10)
    mock_server.async_ensure_running.assert_awaited_with(restart=True)


async def test_spawned_restart_fails(
    hass: HomeAssistant,
    spawned: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A restart that fails is retried later."""
    mock_server.async_ensure_running.side_effect = [LotseServerError("exited"), "s"]
    mock_server.exit(1)
    await run_tasks(hass)
    assert _connection(spawned).client is None
    await _tick(hass, freezer, 1)
    assert _connection(spawned).client is mock_clients.last
    assert mock_clients.paths[-1] == "s"
