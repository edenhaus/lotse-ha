"""Tests for running the lotse daemon."""

import asyncio
from collections.abc import Generator
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from lotse_client import ApiVersionError, CannotConnectError
import pytest

from custom_components.lotse.server import (
    LINE_LIMIT,
    TEXT_LIMIT,
    LotseServer,
    LotseServerError,
    async_get_version,
)
from homeassistant.core import HomeAssistant

from . import run_tasks
from .conftest import BINARY_PATH, MockClients

SERVER = "custom_components.lotse.server"


class FakeProcess:
    """A spawned daemon process."""

    def __init__(self, pid: int = 4242) -> None:
        """Start running."""
        self.pid = pid
        self.returncode: int | None = None
        self.stderr = asyncio.StreamReader(limit=LINE_LIMIT)
        self.exit_on_terminate = True
        self._exited = asyncio.Event()
        self.terminate = Mock(side_effect=self._terminate)
        self.kill = Mock(side_effect=lambda: self.exit(-9))

    def _terminate(self) -> None:
        if self.exit_on_terminate:
            self.exit(0)

    def exit(self, code: int) -> None:
        """End the process with `code`."""
        if self.returncode is None:
            self.returncode = code
            self.stderr.feed_eof()
            self._exited.set()

    async def wait(self) -> int:
        """Wait for the exit."""
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def spawn_dir(tmp_path: Path) -> Generator[Path]:
    """The directory `mkdtemp` makes for a spawn."""
    directory = tmp_path / "lotse-spawn"

    def _mkdtemp(**kwargs: Any) -> str:
        directory.mkdir(mode=0o700)
        return str(directory)

    with patch(f"{SERVER}.mkdtemp", side_effect=_mkdtemp):
        yield directory


@pytest.fixture
def process() -> FakeProcess:
    """The daemon process."""
    return FakeProcess()


@pytest.fixture
def create_subprocess(process: FakeProcess) -> Generator[AsyncMock]:
    """Patch spawning a process."""
    with patch(
        f"{SERVER}.asyncio.create_subprocess_exec", return_value=process
    ) as create:
        yield create


@pytest.fixture(autouse=True)
def fast_retry() -> Generator[None]:
    """Probe readiness without waiting between attempts."""
    with patch(f"{SERVER}.READY_RETRY", 0):
        yield


@pytest.fixture
async def server(
    hass: HomeAssistant,
    spawn_dir: Path,
    create_subprocess: AsyncMock,
    mock_clients: MockClients,
) -> LotseServer:
    """A server that is not started yet."""
    return LotseServer(hass, BINARY_PATH)


async def _hang() -> None:
    """Never return."""
    await asyncio.Event().wait()


def _version_process(stdout: bytes, returncode: int) -> AsyncMock:
    """A process that prints `stdout` and exits with `returncode`."""
    process = AsyncMock()
    process.communicate.return_value = (stdout, None)
    process.returncode = returncode
    return process


async def test_get_version() -> None:
    """`lotse --version` prints the version after the name."""
    process = _version_process(b"lotse 0.1.0\n", 0)
    with patch(f"{SERVER}.asyncio.create_subprocess_exec", return_value=process) as run:
        assert await async_get_version(BINARY_PATH) == "0.1.0"
    assert run.call_args.args == (BINARY_PATH, "--version")


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        (b"lotse 0.1.0\n", 1),
        (b"other 0.1.0\n", 0),
        (b"lotse\n", 0),
        (b"", 0),
    ],
)
async def test_get_version_not_lotse(stdout: bytes, returncode: int) -> None:
    """A binary that does not print a lotse version is refused."""
    process = _version_process(stdout, returncode)
    with (
        patch(f"{SERVER}.asyncio.create_subprocess_exec", return_value=process),
        pytest.raises(LotseServerError, match="is not a lotse binary"),
    ):
        await async_get_version(BINARY_PATH)


async def test_get_version_cannot_run() -> None:
    """A binary that cannot be run is refused."""
    with (
        patch(
            f"{SERVER}.asyncio.create_subprocess_exec",
            side_effect=PermissionError("denied"),
        ),
        pytest.raises(LotseServerError, match="cannot run"),
    ):
        await async_get_version(BINARY_PATH)


@pytest.mark.parametrize("kill_error", [None, ProcessLookupError()])
async def test_get_version_timeout(kill_error: BaseException | None) -> None:
    """A binary that does not finish in time is killed and refused."""
    process = AsyncMock()
    process.communicate.side_effect = _hang
    process.kill = Mock(side_effect=kill_error)
    with (
        patch(f"{SERVER}.asyncio.create_subprocess_exec", return_value=process),
        patch(f"{SERVER}.VERSION_TIMEOUT", 0.01),
        pytest.raises(LotseServerError, match="did not finish"),
    ):
        await async_get_version(BINARY_PATH)
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_start_and_stop(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    create_subprocess: AsyncMock,
    process: FakeProcess,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The daemon is spawned in its own directory, probed until ready, then stopped."""
    caplog.set_level(logging.DEBUG)
    socket = str(spawn_dir / "lotse.sock")
    mock_clients.connect_errors = [CannotConnectError("not yet")]

    assert server.pid is None
    assert not server.running
    assert await server.async_start() == socket

    assert create_subprocess.call_args.args == (
        BINARY_PATH,
        "serve",
        "--socket",
        socket,
        "--log-format",
        "json",
    )
    assert server.running
    assert server.pid == 4242
    assert mock_clients.paths == [socket, socket]
    mock_clients.last.close.assert_awaited_once()
    assert "lotse is not ready yet: not yet" in caplog.text
    assert "lotse 0.1.0 is ready, pid 4242, API 0.1.0" in caplog.text

    await server.async_stop()
    process.terminate.assert_called_once()
    process.kill.assert_not_called()
    assert not spawn_dir.exists()
    assert not server.running
    assert "lotse (pid 4242) stopped with code 0" in caplog.text
    assert f"Removed {spawn_dir}" in caplog.text


async def test_ensure_running(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    create_subprocess: AsyncMock,
    process: FakeProcess,
) -> None:
    """A running daemon is kept unless a restart is asked for."""
    socket = str(spawn_dir / "lotse.sock")
    assert await server.async_ensure_running() == socket
    assert create_subprocess.call_count == 1

    assert await server.async_ensure_running() == socket
    assert create_subprocess.call_count == 1

    create_subprocess.return_value = FakeProcess(pid=4343)
    assert await server.async_ensure_running(restart=True) == socket
    assert create_subprocess.call_count == 2
    process.terminate.assert_called_once()
    assert server.pid == 4343

    create_subprocess.return_value.exit(1)
    await run_tasks(hass)
    create_subprocess.return_value = FakeProcess(pid=4444)
    assert await server.async_ensure_running() == socket
    assert server.pid == 4444
    await server.async_stop()


async def test_unexpected_exit(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    process: FakeProcess,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A daemon that exits on its own is reported to whoever waits for it."""
    assert await server.async_wait() is None
    await server.async_start()
    waiter = asyncio.create_task(server.async_wait())
    await run_tasks(hass)
    assert not waiter.done()

    process.exit(101)
    assert await waiter == 101
    assert not server.running
    assert "lotse (pid 4242) exited unexpectedly with code 101" in caplog.text

    await server.async_stop()
    process.terminate.assert_not_called()
    assert not spawn_dir.exists()


async def test_stop_kills_after_timeout(
    hass: HomeAssistant,
    server: LotseServer,
    process: FakeProcess,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A daemon that ignores SIGTERM gets SIGKILL."""
    await server.async_start()
    process.exit_on_terminate = False
    with patch(f"{SERVER}.TERMINATE_TIMEOUT", 0.01):
        await server.async_stop()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.returncode == -9
    assert "did not stop within 0.01 s after SIGTERM, killing it" in caplog.text


async def test_stop_process_gone(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    process: FakeProcess,
) -> None:
    """A process that is gone when it is signalled does not fail the stop."""
    await server.async_start()
    process.exit_on_terminate = False
    process.terminate.side_effect = ProcessLookupError

    def _gone() -> None:
        process.exit(0)
        raise ProcessLookupError

    process.kill.side_effect = _gone
    with patch(f"{SERVER}.TERMINATE_TIMEOUT", 0.01):
        await server.async_stop()
    process.kill.assert_called_once()
    assert not spawn_dir.exists()


async def test_stop_not_started(hass: HomeAssistant, server: LotseServer) -> None:
    """Stopping a server that never started does nothing."""
    await server.async_stop()
    assert server.pid is None


async def test_remove_directory_fails(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A spawn directory that cannot be removed is reported."""
    await server.async_start()
    with patch(f"{SERVER}.shutil.rmtree", side_effect=OSError("busy")):
        await server.async_stop()
    assert f"Could not remove {spawn_dir}: busy" in caplog.text


async def test_start_cannot_run(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    create_subprocess: AsyncMock,
) -> None:
    """A binary that cannot be run fails the start and leaves no directory."""
    create_subprocess.side_effect = FileNotFoundError("no such file")
    with pytest.raises(LotseServerError, match="cannot run /usr/bin/lotse"):
        await server.async_start()
    assert not spawn_dir.exists()


async def test_start_exits_before_ready(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    process: FakeProcess,
    mock_clients: MockClients,
) -> None:
    """A daemon that exits before it is ready fails the start."""
    process.exit(2)
    with pytest.raises(
        LotseServerError, match="exited with code 2 before it was ready"
    ):
        await server.async_start()
    assert not mock_clients.clients
    assert not spawn_dir.exists()
    assert not server.running


async def test_start_not_ready_in_time(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    process: FakeProcess,
    mock_clients: MockClients,
) -> None:
    """A daemon that does not answer in time is stopped."""
    mock_clients.connect_errors = [CannotConnectError("refused")] * 10_000
    with (
        patch(f"{SERVER}.READY_TIMEOUT", 0.05),
        patch(f"{SERVER}.READY_RETRY", 0.001),
        pytest.raises(LotseServerError, match="not ready within"),
    ):
        await server.async_start()
    process.terminate.assert_called_once()
    assert not spawn_dir.exists()


async def test_start_api_version(
    hass: HomeAssistant,
    server: LotseServer,
    spawn_dir: Path,
    process: FakeProcess,
    mock_clients: MockClients,
) -> None:
    """A daemon of an API version the client does not speak is stopped at once."""
    mock_clients.connect_errors = [ApiVersionError("0.2.0", "0.1.0")]
    with pytest.raises(ApiVersionError):
        await server.async_start()
    assert len(mock_clients.clients) == 1
    process.terminate.assert_called_once()
    assert not spawn_dir.exists()


def _line(record: Any) -> bytes:
    return json.dumps(record).encode() + b"\n"


async def test_log_relay(
    hass: HomeAssistant,
    server: LotseServer,
    process: FakeProcess,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The daemon's JSON lines are logged at their own level, anything else at debug."""
    caplog.set_level(logging.DEBUG, logger="custom_components.lotse.daemon")
    await server.async_start()
    lines = [
        _line(
            {
                "timestamp": "2026-11-06T10:00:00Z",
                "level": "INFO",
                "message": "supervisor ready",
                "event": "ready",
                "max_streams": 256,
                "spans": [{"name": "process", "kind": "supervisor", "pid": 4242}],
            }
        ),
        _line({"level": "WARN", "message": "source slow", "stream_id": "camera.a"}),
        _line({"level": "ERROR", "message": "worker crashed", "spans": ["x"]}),
        _line({"level": "DEBUG", "message": "debug line"}),
        _line({"level": "TRACE", "message": "trace line"}),
        _line({"level": "LOUD", "message": "unknown level"}),
        _line({"level": ["INFO"], "message": "level not a string"}),
        _line(["not", "an", "object"]),
        b"panicked at rtsp://user:pass@host\n",
        b"x" * (LINE_LIMIT + 10) + b"\n",
        _line({"level": "INFO", "message": "y" * (TEXT_LIMIT + 100)}),
    ]
    for line in lines:
        process.stderr.feed_data(line)
    await run_tasks(hass)

    records = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "custom_components.lotse.daemon"
    ]
    assert records[:5] == [
        (
            logging.INFO,
            "[supervisor 4242] supervisor ready event=ready max_streams=256",
        ),
        (logging.WARNING, "source slow stream_id=camera.a"),
        (logging.ERROR, "worker crashed"),
        (logging.DEBUG, "debug line"),
        (logging.DEBUG, "trace line"),
    ]
    unparsed = [message for level, message in records[5:9]]
    assert all(level == logging.DEBUG for level, _ in records[5:9])
    assert unparsed[0].startswith('Unparsed output: {"level": "LOUD"')
    assert unparsed[1].startswith('Unparsed output: {"level": ["INFO"]')
    assert unparsed[2] == 'Unparsed output: ["not", "an", "object"]'
    assert unparsed[3] == "Unparsed output: panicked at rtsp://user:pass@host"
    assert records[9] == (
        logging.DEBUG,
        f"Dropped an output line longer than {LINE_LIMIT} bytes",
    )
    assert records[10] == (logging.INFO, "y" * TEXT_LIMIT)
    assert not [
        record
        for record in caplog.records
        if "user:pass" in record.getMessage() and record.levelno > logging.DEBUG
    ]
    await server.async_stop()
