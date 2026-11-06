"""Tests for the repair issues from lotse's sandbox report."""

from typing import Any

from lotse_client.models import InfoResult
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lotse.const import DOMAIN
from custom_components.lotse.issues import (
    ISSUE_SANDBOX_DEGRADED,
    ISSUE_SANDBOX_OFF,
    LEARN_MORE_URL,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import run_tasks
from .conftest import INFO, MockClients


def _info(**sandbox: Any) -> InfoResult:
    """`info` with these sandbox fields changed."""
    data = INFO.to_dict()
    landlock = sandbox.pop("landlock", {})
    data["sandbox"] = {**data["sandbox"], **sandbox}
    data["sandbox"]["landlock"] = {**data["sandbox"]["landlock"], **landlock}
    return InfoResult.from_dict(data)


def _with_info(info: InfoResult) -> Any:
    return lambda client: setattr(client.info, "return_value", info)


async def _setup(
    hass: HomeAssistant, entry: MockConfigEntry, clients: MockClients, info: InfoResult
) -> None:
    clients.configure = [_with_info(info)]
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_sandbox_in_place(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    init_integration: MockConfigEntry,
) -> None:
    """A daemon with every layer raises no issue; Landlock's network rules do not count."""
    assert INFO.sandbox.landlock.net == "unavailable"
    assert not issue_registry.issues


async def test_sandbox_off(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
    mock_clients: MockClients,
) -> None:
    """A daemon with its sandbox off raises an issue."""
    info = _info(mode="off", uid=0, no_new_privs=False, seccomp="off")
    await _setup(hass, mock_config_entry, mock_clients, info)
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_SANDBOX_OFF)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    assert issue.learn_more_url == LEARN_MORE_URL
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_SANDBOX_DEGRADED) is None


@pytest.mark.parametrize(
    ("sandbox", "layers"),
    [
        ({"uid": 0}, "privilege drop (runs as root)"),
        ({"no_new_privs": False}, "no_new_privs"),
        ({"seccomp": "unavailable"}, "seccomp (unavailable)"),
        ({"landlock": {"fs": "off"}}, "Landlock filesystem rules (off)"),
        (
            {"uid": 0, "seccomp": "off", "landlock": {"fs": "unavailable"}},
            "privilege drop (runs as root), seccomp (off), "
            "Landlock filesystem rules (unavailable)",
        ),
    ],
)
async def test_sandbox_degraded(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
    sandbox: dict[str, Any],
    layers: str,
) -> None:
    """A daemon without a layer raises an issue naming the missing layers."""
    await _setup(hass, mock_config_entry, mock_clients, _info(**sandbox))
    issue = issue_registry.async_get_issue(DOMAIN, ISSUE_SANDBOX_DEGRADED)
    assert issue is not None
    assert issue.translation_placeholders == {"layers": layers}
    assert issue.learn_more_url == LEARN_MORE_URL
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_SANDBOX_OFF) is None
    assert f"lotse runs without these sandbox layers: {layers}" in caplog.text


async def test_issues_follow_reconnects(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
    mock_clients: MockClients,
) -> None:
    """Each connect raises the issues that apply and withdraws the others."""
    await _setup(hass, mock_config_entry, mock_clients, _info(mode="off"))
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_SANDBOX_OFF)

    mock_clients.configure = [_with_info(_info(seccomp="off"))]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_SANDBOX_OFF) is None
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_SANDBOX_DEGRADED)

    mock_clients.configure = [_with_info(INFO)]
    mock_clients.last.closed.set_result(None)
    await run_tasks(hass)
    assert not issue_registry.issues


async def test_issues_withdrawn_on_unload(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    mock_config_entry: MockConfigEntry,
    mock_clients: MockClients,
) -> None:
    """Unloading the entry withdraws its issues."""
    await _setup(hass, mock_config_entry, mock_clients, _info(mode="off"))
    assert issue_registry.issues
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert not issue_registry.issues
