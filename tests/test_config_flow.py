"""Tests for the lotse config flow."""

from collections.abc import Generator
from pathlib import Path
import stat
from unittest.mock import AsyncMock, patch

from lotse_client import ApiVersionError, CannotConnectError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lotse.const import (
    CONF_BINARY,
    CONF_MODE,
    CONF_SOCKET,
    DEFAULT_SOCKET,
    DOMAIN,
    MODE_CONNECT,
    MODE_SPAWN,
)
from custom_components.lotse.server import LotseServerError
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import SOCKET_PATH, MockClients

pytestmark = pytest.mark.usefixtures("mock_setup_entry")


@pytest.fixture
def binary(tmp_path: Path) -> str:
    """An executable file."""
    path = tmp_path / "lotse"
    path.write_text("#!/bin/sh\n")
    path.chmod(stat.S_IRWXU)
    return str(path)


@pytest.fixture
def mock_version() -> Generator[AsyncMock]:
    """Patch running `lotse --version`."""
    with patch(
        "custom_components.lotse.config_flow.async_get_version", return_value="0.1.0"
    ) as get_version:
        yield get_version


@pytest.fixture
def mock_which(binary: str) -> Generator[None]:
    """Patch finding lotse on the PATH."""
    with patch("custom_components.lotse.config_flow.shutil.which", return_value=binary):
        yield


def _suggested(result: dict, key: str) -> str | None:
    """The suggested value of a form field."""
    for marker in result["data_schema"].schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    raise KeyError(key)


@pytest.mark.usefixtures("mock_which")
async def test_spawn(hass: HomeAssistant, binary: str, mock_version: AsyncMock) -> None:
    """Choosing to run lotse checks the binary and creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [MODE_SPAWN, MODE_CONNECT]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_SPAWN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == MODE_SPAWN
    assert _suggested(result, CONF_BINARY) == binary

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BINARY: binary}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "lotse"
    assert result["data"] == {CONF_MODE: MODE_SPAWN, CONF_BINARY: binary}
    mock_version.assert_awaited_once_with(binary)


async def test_spawn_not_on_path(hass: HomeAssistant) -> None:
    """Without lotse on the PATH nothing is suggested."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.lotse.config_flow.shutil.which", return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": MODE_SPAWN}
        )
    assert result["type"] is FlowResultType.FORM
    assert _suggested(result, CONF_BINARY) is None


@pytest.mark.usefixtures("mock_which")
async def test_spawn_errors(
    hass: HomeAssistant, tmp_path: Path, binary: str, mock_version: AsyncMock
) -> None:
    """Each binary error is shown, and the flow recovers from it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_SPAWN}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BINARY: str(tmp_path / "missing")}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_BINARY: "binary_not_found"}
    assert _suggested(result, CONF_BINARY) == str(tmp_path / "missing")

    not_executable = tmp_path / "not-executable"
    not_executable.write_text("")
    not_executable.chmod(stat.S_IRUSR)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BINARY: str(not_executable)}
    )
    assert result["errors"] == {CONF_BINARY: "binary_not_executable"}

    mock_version.side_effect = LotseServerError("not lotse")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BINARY: binary}
    )
    assert result["errors"] == {CONF_BINARY: "invalid_binary"}

    mock_version.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BINARY: binary}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_MODE: MODE_SPAWN, CONF_BINARY: binary}


async def test_connect(hass: HomeAssistant, mock_clients: MockClients) -> None:
    """Choosing a running lotse checks its socket and creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_CONNECT}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == MODE_CONNECT
    assert _suggested(result, CONF_SOCKET) == DEFAULT_SOCKET

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: SOCKET_PATH}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "lotse"
    assert result["data"] == {CONF_MODE: MODE_CONNECT, CONF_SOCKET: SOCKET_PATH}
    assert mock_clients.paths == [SOCKET_PATH]
    mock_clients.last.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (CannotConnectError("refused"), "cannot_connect"),
        (ApiVersionError("0.2.0", "0.1.0"), "api_version"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_connect_errors(
    hass: HomeAssistant,
    mock_clients: MockClients,
    error: BaseException,
    reason: str,
) -> None:
    """A socket that does not answer as lotse shows an error, and the flow recovers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_CONNECT}
    )
    mock_clients.connect_errors = [error]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: SOCKET_PATH}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}
    assert _suggested(result, CONF_SOCKET) == SOCKET_PATH

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: SOCKET_PATH}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_single_instance(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Only one entry can exist."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reconfigure_connect(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_clients: MockClients
) -> None:
    """Reconfiguring keeps the mode and suggests the current socket."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_CONNECT}
    )
    assert _suggested(result, CONF_SOCKET) == SOCKET_PATH

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOCKET: "/run/other/lotse.sock"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {
        CONF_MODE: MODE_CONNECT,
        CONF_SOCKET: "/run/other/lotse.sock",
    }


@pytest.mark.usefixtures("mock_version")
async def test_reconfigure_to_spawn(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    binary: str,
) -> None:
    """Reconfiguring can switch from connecting to running lotse."""
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reconfigure_flow(hass)
    with patch("custom_components.lotse.config_flow.shutil.which", return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": MODE_SPAWN}
        )
    assert _suggested(result, CONF_BINARY) is None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BINARY: binary}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {CONF_MODE: MODE_SPAWN, CONF_BINARY: binary}


async def test_reconfigure_spawn(
    hass: HomeAssistant, mock_spawn_entry: MockConfigEntry
) -> None:
    """Reconfiguring a spawn entry suggests its binary."""
    mock_spawn_entry.add_to_hass(hass)
    result = await mock_spawn_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_SPAWN}
    )
    assert _suggested(result, CONF_BINARY) == mock_spawn_entry.data[CONF_BINARY]
