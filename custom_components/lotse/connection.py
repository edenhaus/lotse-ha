"""The control connection to the lotse daemon, supervised.

There is one connection for the daemon's whole life. It is pinged every
`PING_INTERVAL` seconds; a ping without a pong within `PING_TIMEOUT`, a
closed connection or, when Home Assistant runs the daemon, the daemon's exit
counts as lost. A lost connection is reopened at once and then with a growing
delay; a spawned daemon is restarted when it exited, did not answer, or a
reconnect to it failed. Listeners hear of every reconnect, so sessions can be
adopted again within the daemon's grace period.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import logging

from lotse_client import LotseClient, LotseError
from lotse_client.models import InfoResult

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN, PING_INTERVAL, PING_TIMEOUT, RECONNECT_MAX_DELAY
from .server import LotseServer, LotseServerError

_LOGGER = logging.getLogger(__name__)

type ConnectedListener = Callable[[LotseClient], Awaitable[None]]
"""Called with the new client after every reconnect."""


def reconnect_delay(attempt: int) -> int:
    """Seconds before reconnect attempt `attempt`; the first one is immediate."""
    if attempt == 0:
        return 0
    # Doubling from 1 s; the shift is bounded so it stays small.
    return min(1 << min(attempt - 1, 16), RECONNECT_MAX_DELAY)


class LotseConnection:
    """The supervised control connection to one lotse daemon."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, endpoint: str | LotseServer
    ) -> None:
        """Prepare; `endpoint` is a control socket's path or a daemon to spawn."""
        self._hass = hass
        self._entry = entry
        self._endpoint = endpoint
        self.client: LotseClient | None = None
        """The open connection; `None` while the daemon cannot be reached."""
        self.info: InfoResult | None = None
        """The daemon's `info`, read on every connect and kept while it is lost."""
        self._listeners: list[ConnectedListener] = []
        self._watchers: list[asyncio.Task[None]] = []
        self._unsub_ping: CALLBACK_TYPE | None = None
        self._unsub_retry: CALLBACK_TYPE | None = None
        self._attempt = 0
        self._restart = False
        self._stopped = False

    @property
    def server(self) -> LotseServer | None:
        """The daemon Home Assistant runs, if it runs one."""
        return self._endpoint if isinstance(self._endpoint, LotseServer) else None

    @callback
    def async_add_listener(self, listener: ConnectedListener) -> None:
        """Call `listener` after every reconnect."""
        self._listeners.append(listener)

    async def async_start(self) -> None:
        """Open the connection, spawning the daemon first if Home Assistant runs it."""
        client, info = await self._async_open(restart=False)
        _LOGGER.info("Connected to lotse %s, API %s", info.version, client.hello.api)
        self._async_connected(client, info)

    async def async_stop(self) -> None:
        """Close the connection for good and stop a spawned daemon."""
        self._stopped = True
        self._async_cancel_supervision()
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None
        client, self.client = self.client, None
        if client is not None:
            await client.close()
        if (server := self.server) is not None:
            await server.async_stop()

    async def _async_open(self, *, restart: bool) -> tuple[LotseClient, InfoResult]:
        """Open a connection and read `info`; spawn or restart the daemon if needed."""
        if (server := self.server) is not None:
            socket_path = await server.async_ensure_running(restart=restart)
        else:
            socket_path = str(self._endpoint)
        client = LotseClient(socket_path)
        await client.connect()
        try:
            info = await client.info()
        except BaseException:
            await client.close()
            raise
        return client, info

    @callback
    def _async_connected(self, client: LotseClient, info: InfoResult) -> None:
        """Start supervising a newly opened connection."""
        self.client = client
        self.info = info
        self._attempt = 0
        self._restart = False
        self._unsub_ping = async_track_time_interval(
            self._hass,
            self._async_ping,
            timedelta(seconds=PING_INTERVAL),
            name=f"{DOMAIN} ping",
            cancel_on_shutdown=True,
        )
        self._watchers.append(
            self._entry.async_create_background_task(
                self._hass, self._async_watch_connection(client), f"{DOMAIN} watch"
            )
        )
        if (server := self.server) is not None:
            self._watchers.append(
                self._entry.async_create_background_task(
                    self._hass,
                    self._async_watch_process(client, server),
                    f"{DOMAIN} watch process",
                )
            )

    @callback
    def _async_cancel_supervision(self) -> None:
        """Stop pinging and watching the current connection."""
        if self._unsub_ping is not None:
            self._unsub_ping()
            self._unsub_ping = None
        watchers, self._watchers = self._watchers, []
        current = asyncio.current_task()
        for task in watchers:
            if task is not current:
                task.cancel()

    async def _async_watch_connection(self, client: LotseClient) -> None:
        """Notice the connection closing."""
        error = await client.wait_closed()
        self._async_lost(client, f"connection closed: {error}", restart=False)

    async def _async_watch_process(
        self, client: LotseClient, server: LotseServer
    ) -> None:
        """Notice the spawned daemon exiting."""
        code = await server.async_wait()
        self._async_lost(client, f"lotse exited with code {code}", restart=True)

    @callback
    def _async_ping(self, _now: datetime) -> None:
        """Send the periodic ping."""
        if (client := self.client) is not None:
            self._entry.async_create_background_task(
                self._hass, self._async_check(client), f"{DOMAIN} ping"
            )

    async def _async_check(self, client: LotseClient) -> None:
        """Ping once; a missing pong means the daemon does not answer."""
        try:
            async with asyncio.timeout(PING_TIMEOUT):
                await client.ping()
        except TimeoutError:
            self._async_lost(client, f"no pong within {PING_TIMEOUT} s", restart=True)
        except LotseError as err:
            self._async_lost(client, f"ping failed: {err}", restart=False)

    @callback
    def _async_lost(self, client: LotseClient, reason: str, *, restart: bool) -> None:
        """Handle the loss of `client`'s connection: close it and reconnect."""
        if client is not self.client or self._stopped:
            return
        _LOGGER.warning("Lost the connection to lotse: %s", reason)
        self.client = None
        self._restart = restart
        self._async_cancel_supervision()
        # Closing orphans the sessions on the daemon, which keeps them for
        # its grace period so a new connection can adopt them.
        self._entry.async_create_background_task(
            self._hass, client.close(), f"{DOMAIN} close"
        )
        self._async_schedule_reconnect()

    @callback
    def _async_schedule_reconnect(self) -> None:
        """Try to reconnect now or after the attempt's delay."""
        delay = reconnect_delay(self._attempt)
        if delay == 0:
            self._entry.async_create_background_task(
                self._hass, self._async_reconnect(), f"{DOMAIN} reconnect"
            )
            return
        _LOGGER.info("Reconnecting to lotse in %s s", delay)
        self._unsub_retry = async_call_later(
            self._hass,
            delay,
            HassJob(self._async_retry, f"{DOMAIN} reconnect", cancel_on_shutdown=True),
        )

    @callback
    def _async_retry(self, _now: datetime) -> None:
        """Run a delayed reconnect attempt."""
        self._unsub_retry = None
        self._entry.async_create_background_task(
            self._hass, self._async_reconnect(), f"{DOMAIN} reconnect"
        )

    async def _async_reconnect(self) -> None:
        """Make one reconnect attempt; schedule the next if it fails."""
        restart = self.server is not None and (self._restart or self._attempt > 0)
        if restart:
            _LOGGER.info("Restarting lotse")
        try:
            client, info = await self._async_open(restart=restart)
        except (LotseError, LotseServerError) as err:
            self._attempt += 1
            _LOGGER.debug("Reconnect attempt %s failed: %s", self._attempt, err)
            if not self._stopped:
                self._async_schedule_reconnect()
            return
        if self._stopped:
            await client.close()
            return
        _LOGGER.info("Reconnected to lotse %s", info.version)
        self._async_connected(client, info)
        for listener in self._listeners:
            await listener(client)
