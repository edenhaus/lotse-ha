"""Diagnostics support for lotse.

Stream source URLs carry camera credentials. The daemon already reports
them without their userinfo, but every URL and credential is redacted here
as well, so a diagnostics download names neither cameras' addresses nor
TURN credentials.
"""

from typing import Any

from lotse_client import LotseError

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import LotseConfigEntry

TO_REDACT = {
    "credential",
    "password",
    "resolved_url",
    "url",
    "urls",
    "username",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LotseConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    connection = entry.runtime_data.connection
    data: dict[str, Any] = {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "connected": connection.client is not None,
        "server_pid": None if connection.server is None else connection.server.pid,
        "sessions": entry.runtime_data.provider.session_count,
        "info": None if connection.info is None else connection.info.to_dict(),
    }
    if (client := connection.client) is not None:
        try:
            data["metrics"] = (await client.metrics.get()).to_dict()
            data["streams"] = (await client.stream.list()).to_dict()["streams"]
        except LotseError as err:
            data["error"] = str(err)
    return async_redact_data(data, TO_REDACT)
