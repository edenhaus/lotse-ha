"""Run the lotse daemon as a child process of Home Assistant.

Each spawn gets a fresh 0700 directory from `mkdtemp`, and the daemon binds
its control socket inside it (`lotse serve --socket <dir>/lotse.sock`). The
daemon counts as ready once a control connection is greeted with `hello`.
It writes one JSON object per line to stderr, which is re-logged here at each
line's level. Stopping it is SIGTERM, then SIGKILL after a grace period; the
spawn directory is removed afterwards.
"""

import asyncio
from contextlib import suppress
from functools import partial
import json
import logging
from pathlib import Path
import shutil
from tempfile import mkdtemp
from typing import Any, Final

from lotse_client import ApiVersionError, LotseClient, LotseError

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import BINARY_NAME, DOMAIN, SOCKET_NAME

_LOGGER = logging.getLogger(__name__)
_DAEMON_LOGGER = logging.getLogger(f"{__package__}.daemon")
"""The logger the daemon's own lines are re-logged on."""

READY_TIMEOUT: Final = 30
"""Seconds a spawned daemon has to answer a control connection with `hello`."""
READY_RETRY: Final = 0.1
"""Seconds between two readiness probes."""
TERMINATE_TIMEOUT: Final = 5
"""Seconds between SIGTERM and SIGKILL."""
VERSION_TIMEOUT: Final = 10
"""Seconds `lotse --version` may take."""
LINE_LIMIT: Final = 64 * 1024
"""The longest stderr line read, in bytes; a longer one is dropped."""
TEXT_LIMIT: Final = 2048
"""The most characters of one daemon line that are re-logged."""

_LEVELS: Final = {
    "TRACE": logging.DEBUG,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}
"""The daemon's log levels, as its JSON lines name them, and Home Assistant's."""
_NOT_FIELDS: Final = frozenset({"timestamp", "level", "message", "spans"})
"""The keys of a JSON line that are not its event's own fields."""


class LotseServerError(HomeAssistantError):
    """The daemon could not be started, or the binary is not lotse."""


async def async_get_version(binary: str) -> str:
    """Run `<binary> --version` and return the version a lotse binary prints."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as err:
        raise LotseServerError(f"cannot run {binary}: {err}") from err
    try:
        async with asyncio.timeout(VERSION_TIMEOUT):
            stdout, _ = await process.communicate()
    except TimeoutError as err:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise LotseServerError(f"{binary} --version did not finish") from err
    name, _, version = stdout.decode(errors="replace").strip().partition(" ")
    if process.returncode != 0 or name != BINARY_NAME or not version:
        raise LotseServerError(f"{binary} is not a lotse binary")
    return version


def _format_value(value: Any) -> str:
    """Render one field of a daemon line."""
    return value if isinstance(value, str) else json.dumps(value)


def _parse_line(line: bytes) -> tuple[int, str] | None:
    """Return the level and text of one JSON line; `None` if it is not one."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    level_name = record.get("level")
    if not isinstance(level_name, str) or (level := _LEVELS.get(level_name)) is None:
        return None
    parts = [_format_value(record.get("message", ""))]
    for span in record.get("spans", ()):
        if isinstance(span, dict) and span.get("name") == "process":
            parts.insert(0, f"[{span.get('kind')} {span.get('pid')}]")
    parts.extend(
        f"{key}={_format_value(value)}"
        for key, value in record.items()
        if key not in _NOT_FIELDS
    )
    return level, " ".join(parts)


def _relog(line: bytes) -> None:
    """Log one stderr line of the daemon at its own level, capped in length."""
    if (parsed := _parse_line(line)) is None:
        # Never above debug: text that is not the daemon's structured log
        # has not passed through its redaction.
        _DAEMON_LOGGER.debug(
            "Unparsed output: %s", line.decode(errors="replace").rstrip()[:TEXT_LIMIT]
        )
        return
    level, text = parsed
    _DAEMON_LOGGER.log(level, "%s", text[:TEXT_LIMIT])


class LotseServer:
    """The lotse daemon, spawned and stopped by Home Assistant."""

    def __init__(self, hass: HomeAssistant, binary: str) -> None:
        """Prepare a server; nothing runs until `async_start`."""
        self._hass = hass
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._directory: str | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[int] | None = None
        self._stopping = False

    @property
    def pid(self) -> int | None:
        """The process id of the running daemon."""
        return None if self._process is None else self._process.pid

    @property
    def running(self) -> bool:
        """Whether the spawned daemon is still running."""
        return self._process is not None and self._process.returncode is None

    async def async_ensure_running(self, *, restart: bool = False) -> str:
        """Start the daemon unless it runs, or restart it; return its socket path."""
        if restart or not self.running or self._directory is None:
            await self.async_stop()
            return await self.async_start()
        return str(Path(self._directory) / SOCKET_NAME)

    async def async_start(self) -> str:
        """Spawn the daemon and wait until it is ready; return its socket path."""
        self._stopping = False
        directory = await self._hass.async_add_executor_job(
            partial(mkdtemp, prefix=f"{DOMAIN}-")
        )
        self._directory = directory
        socket = str(Path(directory) / SOCKET_NAME)
        try:
            process = await asyncio.create_subprocess_exec(
                self._binary,
                "serve",
                "--socket",
                socket,
                "--log-format",
                "json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT,
            )
        except OSError as err:
            await self._async_remove_directory()
            raise LotseServerError(f"cannot run {self._binary}: {err}") from err
        self._process = process
        _LOGGER.info("Started lotse, pid %s", process.pid)
        if process.stderr is not None:
            self._log_task = self._hass.async_create_background_task(
                self._async_relay_log(process.stderr), f"{DOMAIN} log relay"
            )
        self._exit_task = self._hass.async_create_background_task(
            self._async_watch_exit(process), f"{DOMAIN} exit watch"
        )
        try:
            await self._async_wait_ready(process, socket)
        except BaseException:
            await self.async_stop()
            raise
        return socket

    async def _async_wait_ready(
        self, process: asyncio.subprocess.Process, socket: str
    ) -> None:
        """Probe the control socket until the daemon greets with `hello`."""
        try:
            async with asyncio.timeout(READY_TIMEOUT):
                while True:
                    if process.returncode is not None:
                        raise LotseServerError(
                            f"lotse exited with code {process.returncode} before it was ready"
                        )
                    client = LotseClient(socket)
                    try:
                        hello = await client.connect()
                    except LotseError as err:
                        if isinstance(err, ApiVersionError):
                            raise
                        _LOGGER.debug("lotse is not ready yet: %s", err)
                        await asyncio.sleep(READY_RETRY)
                        continue
                    await client.close()
                    _LOGGER.info(
                        "lotse %s is ready, pid %s, API %s",
                        hello.version,
                        process.pid,
                        hello.api,
                    )
                    return
        except TimeoutError as err:
            raise LotseServerError(
                f"lotse was not ready within {READY_TIMEOUT} s"
            ) from err

    async def _async_relay_log(self, stream: asyncio.StreamReader) -> None:
        """Re-log the daemon's stderr until it closes."""
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                _DAEMON_LOGGER.debug(
                    "Dropped an output line longer than %s bytes", LINE_LIMIT
                )
                continue
            if not line:
                return
            _relog(line)

    async def _async_watch_exit(self, process: asyncio.subprocess.Process) -> int:
        """Wait for the process to end and log how it ended."""
        code = await process.wait()
        if self._stopping:
            _LOGGER.info("lotse (pid %s) stopped with code %s", process.pid, code)
        else:
            _LOGGER.warning(
                "lotse (pid %s) exited unexpectedly with code %s", process.pid, code
            )
        return code

    async def async_wait(self) -> int | None:
        """Wait until the running daemon exits; its exit code, `None` if none runs."""
        if (task := self._exit_task) is None:
            return None
        return await asyncio.shield(task)

    async def async_stop(self) -> None:
        """Stop the daemon: SIGTERM, then SIGKILL; then remove its directory."""
        self._stopping = True
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            _LOGGER.debug("Stopping lotse, pid %s", process.pid)
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                async with asyncio.timeout(TERMINATE_TIMEOUT):
                    await process.wait()
            except TimeoutError:
                _LOGGER.warning(
                    "lotse (pid %s) did not stop within %s s after SIGTERM, killing it",
                    process.pid,
                    TERMINATE_TIMEOUT,
                )
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        # The exit watch ends with the process and logs its code; the log relay
        # may wait on a pipe a worker still holds open, so it is cancelled.
        if self._log_task is not None:
            self._log_task.cancel()
        tasks = [task for task in (self._log_task, self._exit_task) if task is not None]
        self._log_task = self._exit_task = None
        if tasks:
            await asyncio.wait(tasks)
        await self._async_remove_directory()

    async def _async_remove_directory(self) -> None:
        """Remove the spawn directory, socket included."""
        if (directory := self._directory) is None:
            return
        self._directory = None
        try:
            await self._hass.async_add_executor_job(shutil.rmtree, directory)
        except OSError as err:
            _LOGGER.warning("Could not remove %s: %s", directory, err)
        else:
            _LOGGER.debug("Removed %s", directory)
