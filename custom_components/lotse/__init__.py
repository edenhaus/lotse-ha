"""The lotse integration: the lotse media daemon as the cameras' WebRTC provider."""

from dataclasses import dataclass
import logging

from lotse_client import ApiVersionError, LotseError

from homeassistant.components.camera import async_register_webrtc_provider
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .connection import LotseConnection
from .const import CONF_BINARY, CONF_MODE, CONF_SOCKET, DOMAIN, MODE_SPAWN
from .provider import LotseWebRTCProvider
from .server import LotseServer, LotseServerError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LotseData:
    """What a loaded entry keeps."""

    connection: LotseConnection
    provider: LotseWebRTCProvider


type LotseConfigEntry = ConfigEntry[LotseData]


async def async_setup_entry(hass: HomeAssistant, entry: LotseConfigEntry) -> bool:
    """Connect to lotse, or run it, and register it as the WebRTC provider."""
    endpoint: str | LotseServer
    if entry.data[CONF_MODE] == MODE_SPAWN:
        endpoint = LotseServer(hass, entry.data[CONF_BINARY])
    else:
        endpoint = entry.data[CONF_SOCKET]
    connection = LotseConnection(hass, entry, endpoint)
    try:
        await connection.async_start()
    except ApiVersionError as err:
        await connection.async_stop()
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="api_version",
            translation_placeholders={"daemon": err.daemon, "client": err.client},
        ) from err
    except (LotseError, LotseServerError) as err:
        await connection.async_stop()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err

    provider = LotseWebRTCProvider(hass, entry, connection)
    connection.async_add_listener(provider.async_on_connected)
    entry.runtime_data = LotseData(connection, provider)

    async def _async_on_stop(_event: Event) -> None:
        """Stop the daemon with Home Assistant."""
        await connection.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop)
    )
    entry.async_on_unload(async_register_webrtc_provider(hass, provider))

    # Sessions the previous entry left behind are adopted on this connection.
    provider.async_take_handover()
    if (client := connection.client) is not None:
        await provider.async_on_connected(client)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LotseConfigEntry) -> bool:
    """Unload the entry; a daemon Home Assistant runs is stopped."""
    data = entry.runtime_data
    # A daemon that keeps running keeps the sessions for its grace period,
    # so the next entry can adopt them.
    data.provider.async_unload(handover=data.connection.server is None)
    await data.connection.async_stop()
    return True
