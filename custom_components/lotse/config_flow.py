"""Config flow for the lotse integration.

lotse is either run by Home Assistant, from a binary the user names, or
already running, reached through its control socket. Both are checked
before an entry is made: the binary must be lotse (`lotse --version`), the
socket must answer with a `hello` this integration understands.
"""

import logging
import os
from pathlib import Path
import shutil
from typing import Any, override

from lotse_client import ApiVersionError, LotseClient, LotseError
import probatio

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)

from .const import (
    BINARY_NAME,
    CONF_BINARY,
    CONF_MODE,
    CONF_SOCKET,
    DEFAULT_SOCKET,
    DOMAIN,
    MODE_CONNECT,
    MODE_SPAWN,
)
from .server import LotseServerError, async_get_version

_LOGGER = logging.getLogger(__name__)

TITLE = "lotse"
MENU_OPTIONS = [MODE_SPAWN, MODE_CONNECT]


def _check_binary(path: str) -> str | None:
    """The error key for a path that is not an executable file."""
    if not Path(path).is_file():
        return "binary_not_found"
    if not os.access(path, os.X_OK):
        return "binary_not_executable"
    return None


async def _async_check_socket(path: str) -> str | None:
    """Connect to a control socket; the error key if that fails."""
    client = LotseClient(path)
    try:
        hello = await client.connect()
    except ApiVersionError as err:
        _LOGGER.debug("lotse at %s speaks an unsupported API: %s", path, err)
        return "api_version"
    except LotseError as err:
        _LOGGER.debug("Cannot connect to lotse at %s: %s", path, err)
        return "cannot_connect"
    except Exception:
        _LOGGER.exception("Unexpected error connecting to lotse at %s", path)
        return "unknown"
    _LOGGER.debug("lotse %s answers at %s", hello.version, path)
    await client.close()
    return None


class LotseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up lotse: run it, or connect to a running one."""

    VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose how to reach lotse."""
        return self.async_show_menu(step_id="user", menu_options=MENU_OPTIONS)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose again how to reach lotse."""
        return self.async_show_menu(step_id="reconfigure", menu_options=MENU_OPTIONS)

    async def async_step_spawn(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Run lotse from a binary."""
        errors: dict[str, str] = {}
        if user_input is not None:
            binary = user_input[CONF_BINARY]
            if error := await self.hass.async_add_executor_job(_check_binary, binary):
                errors[CONF_BINARY] = error
            else:
                try:
                    version = await async_get_version(binary)
                except LotseServerError as err:
                    _LOGGER.debug("%s is not usable: %s", binary, err)
                    errors[CONF_BINARY] = "invalid_binary"
                else:
                    _LOGGER.debug("%s is lotse %s", binary, version)
                    return self._async_finish({CONF_MODE: MODE_SPAWN, **user_input})
        elif (user_input := self._suggested(MODE_SPAWN)) is None and (
            found := await self.hass.async_add_executor_job(shutil.which, BINARY_NAME)
        ):
            user_input = {CONF_BINARY: found}
        return self.async_show_form(
            step_id=MODE_SPAWN,
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema({probatio.Required(CONF_BINARY): str}), user_input
            ),
            errors=errors,
        )

    async def async_step_connect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect to a running lotse through its control socket."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := await _async_check_socket(user_input[CONF_SOCKET]):
                errors["base"] = error
            else:
                return self._async_finish({CONF_MODE: MODE_CONNECT, **user_input})
        else:
            user_input = self._suggested(MODE_CONNECT) or {CONF_SOCKET: DEFAULT_SOCKET}
        return self.async_show_form(
            step_id=MODE_CONNECT,
            data_schema=self.add_suggested_values_to_schema(
                probatio.Schema({probatio.Required(CONF_SOCKET): str}), user_input
            ),
            errors=errors,
        )

    def _suggested(self, mode: str) -> dict[str, Any] | None:
        """The values of the entry being reconfigured, if it uses `mode`."""
        if self.source != SOURCE_RECONFIGURE:
            return None
        data = self._get_reconfigure_entry().data
        return dict(data) if data[CONF_MODE] == mode else None

    def _async_finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry, or update the one being reconfigured."""
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=data
            )
        return self.async_create_entry(title=TITLE, data=data)
