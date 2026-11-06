"""Repair issues from the daemon's sandbox report (`info.sandbox`).

lotse hardens each of its processes in layers: it drops root, sets
`no_new_privs`, and applies a seccomp allowlist and Landlock filesystem rules.
A sandbox that is off, or a layer the host kernel or the daemon's start did
not allow, is raised as a repair issue on every connect and withdrawn once
the daemon reports it in place. Landlock's network rules are not checked:
they need Landlock ABI 4, which many supported kernels lack.
"""

import logging
from typing import Final

from lotse_client.models import SandboxInfo

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ISSUE_SANDBOX_OFF: Final = "sandbox_off"
ISSUE_SANDBOX_DEGRADED: Final = "sandbox_degraded"
LEARN_MORE_URL: Final = "https://github.com/edenhaus/lotse#readme"

ENFORCED: Final = "enforced"
"""The state of a seccomp or Landlock layer that is applied."""
SANDBOX_OFF: Final = "off"
"""The sandbox mode that applies no layer."""


def missing_layers(sandbox: SandboxInfo) -> list[str]:
    """The hardening layers the daemon runs without."""
    missing: list[str] = []
    if sandbox.uid == 0:
        missing.append("privilege drop (runs as root)")
    if not sandbox.no_new_privs:
        missing.append("no_new_privs")
    if sandbox.seccomp != ENFORCED:
        missing.append(f"seccomp ({sandbox.seccomp})")
    if sandbox.landlock.fs != ENFORCED:
        missing.append(f"Landlock filesystem rules ({sandbox.landlock.fs})")
    return missing


@callback
def async_update_sandbox_issues(hass: HomeAssistant, sandbox: SandboxInfo) -> None:
    """Raise the issues the sandbox report calls for, withdraw the others."""
    if sandbox.mode == SANDBOX_OFF:
        _LOGGER.warning("lotse runs with its sandbox off")
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_SANDBOX_OFF,
            is_fixable=False,
            learn_more_url=LEARN_MORE_URL,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SANDBOX_OFF,
        )
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX_DEGRADED)
        return
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX_OFF)
    if missing := missing_layers(sandbox):
        layers = ", ".join(missing)
        _LOGGER.warning("lotse runs without these sandbox layers: %s", layers)
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_SANDBOX_DEGRADED,
            is_fixable=False,
            learn_more_url=LEARN_MORE_URL,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SANDBOX_DEGRADED,
            translation_placeholders={"layers": layers},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX_DEGRADED)


@callback
def async_delete_sandbox_issues(hass: HomeAssistant) -> None:
    """Withdraw every sandbox issue."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX_OFF)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SANDBOX_DEGRADED)
