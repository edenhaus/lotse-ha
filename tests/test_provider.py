"""Tests for lotse as the cameras' WebRTC provider."""

import asyncio
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import MagicMock, Mock, call

from freezegun.api import FrozenDateTimeFactory
from lotse_client import (
    CannotConnectError,
    CommandError,
    ConnectionClosedError,
    Subscription,
)
from lotse_client.models import (
    HttpSource,
    HttpsSource,
    IceServer,
    RtspSource,
    RtspsSource,
    SessionEvent,
    SessionEventAnswer,
    SessionEventCandidate,
    SessionEventClosed,
    SessionEventSession,
    SessionEventState,
    SessionEventWarning,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator
from webrtc_models import RTCConfiguration, RTCIceCandidateInit, RTCIceServer

from custom_components.lotse.const import (
    ERROR_NO_STREAM_SOURCE,
    ERROR_UNAVAILABLE,
    ERROR_UNSUPPORTED_STREAM_SOURCE,
)
from custom_components.lotse.provider import (
    MAX_PENDING_CANDIDATES,
    MAX_STREAM_ID,
    LotseWebRTCProvider,
    ice_servers_for,
    source_for,
    stream_id_for,
)
from homeassistant.components.camera import (
    StreamType,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCClientConfiguration,
    WebRTCError,
)
from homeassistant.components.stream import Orientation
from homeassistant.core import HomeAssistant

from . import STREAM_SOURCE, MockCamera, run_tasks
from .conftest import MockClients, async_setup_camera

OFFER_SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n"
ANSWER_SDP = "v=0\r\no=- 2 2 IN IP4 192.0.2.1\r\ns=lotse\r\n"
CANDIDATE = "candidate:1 1 udp 2130706431 192.0.2.1 18555 typ host"
SESSION_ID = "01J8ZKSESSION"
STREAM_ID = "camera.front_door"


def _provider(entry: MockConfigEntry) -> LotseWebRTCProvider:
    return entry.runtime_data.provider


async def _offer(
    camera: MockCamera, clients: MockClients, session_id: str = SESSION_ID
) -> tuple[Mock, Subscription[SessionEvent] | None]:
    """Send an offer; return the session's `send_message` and its subscription."""
    send_message = Mock()
    subscriptions = clients.last.subscriptions
    opened = len(subscriptions)
    await camera.async_handle_async_webrtc_offer(OFFER_SDP, session_id, send_message)
    return send_message, subscriptions[-1] if len(subscriptions) > opened else None


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await run_tasks(hass)


def _drop(client: MagicMock) -> None:
    """Drop a connection as the client does: its subscriptions fail too."""
    for subscription in client.subscriptions:
        subscription.finish(ConnectionClosedError("the daemon closed the connection"))
    client.closed.set_result(ConnectionClosedError("the daemon closed the connection"))


async def test_provider_registered(
    hass: HomeAssistant, init_integration: MockConfigEntry, camera: MockCamera
) -> None:
    """The camera is served by lotse over WebRTC."""
    assert camera.webrtc_provider is _provider(init_integration)
    assert camera.webrtc_provider.domain == "lotse"
    assert StreamType.WEB_RTC in camera.camera_capabilities.frontend_stream_types


@pytest.mark.parametrize(
    ("stream_source", "supported"),
    [
        ("rtsp://192.0.2.10/h264", True),
        ("RTSPS://192.0.2.10/h264", True),
        # A protocol lotse reads from a URL, but this build did not register.
        ("http://192.0.2.10/stream.m3u8", False),
        # A protocol this build registered, but whose signaling the client
        # relays: it takes no URL.
        ("webrtc://192.0.2.10/stream", False),
        ("no scheme", False),
    ],
)
async def test_is_supported(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    stream_source: str,
    supported: bool,
) -> None:
    """A stream source is supported when the daemon reads URLs of its scheme."""
    provider = _provider(init_integration)
    assert provider.async_is_supported(stream_source) is supported
    init_integration.runtime_data.connection.info = None
    assert not provider.async_is_supported(stream_source)


@pytest.mark.parametrize(
    ("stream_source", "expected"),
    [
        ("rtsp://192.0.2.10/h264", RtspSource(url="rtsp://192.0.2.10/h264")),
        ("RTSPS://192.0.2.10/h264", RtspsSource(url="RTSPS://192.0.2.10/h264")),
        ("http://192.0.2.10/a.m3u8", HttpSource(url="http://192.0.2.10/a.m3u8")),
        ("https://192.0.2.10/a.m3u8", HttpsSource(url="https://192.0.2.10/a.m3u8")),
        ("webrtc://192.0.2.10/stream", None),
        ("no scheme", None),
    ],
)
def test_source_for(
    stream_source: str,
    expected: RtspSource | RtspsSource | HttpSource | HttpsSource | None,
) -> None:
    """A stream source URL becomes the lotse source of its scheme."""
    assert source_for(stream_source) == expected


async def test_offer(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An offer puts the stream, opens a session and relays its events."""
    caplog.set_level(logging.DEBUG)
    client = mock_clients.last
    send_message, subscription = await _offer(camera, mock_clients)
    assert subscription is not None

    client.stream.put.assert_awaited_once_with(
        stream_id=STREAM_ID,
        sources=[RtspSource(url=STREAM_SOURCE)],
        orientation="no_transform",
        preload=False,
    )
    ice_servers = ice_servers_for(camera)
    assert ice_servers
    client.webrtc.offer.assert_awaited_once_with(
        stream_id=STREAM_ID,
        sdp=OFFER_SDP,
        session_id=SESSION_ID,
        ice_servers=ice_servers,
    )
    assert _provider(init_integration).session_count == 1

    for event in (
        SessionEventSession(session_id=SESSION_ID),
        SessionEventAnswer(sdp=ANSWER_SDP),
        SessionEventCandidate(candidate=CANDIDATE, sdp_mid="0", sdp_mline_index=0),
        SessionEventCandidate(candidate=CANDIDATE, sdp_mid="1"),
        SessionEventCandidate(candidate=""),
        SessionEventState(ice="connected", dtls="connected"),
        SessionEventWarning(code="audio_codec_unsupported", message="no AAC"),
        SessionEventClosed(code="peer_closed", message="the browser left"),
    ):
        subscription.deliver(event)
    await run_tasks(hass)

    assert send_message.call_args_list == [
        call(WebRTCAnswer(ANSWER_SDP)),
        call(
            WebRTCCandidate(
                RTCIceCandidateInit(CANDIDATE, sdp_mid="0", sdp_m_line_index=0)
            )
        ),
        call(WebRTCCandidate(RTCIceCandidateInit(CANDIDATE, sdp_mid="1"))),
        call(WebRTCCandidate(RTCIceCandidateInit(""))),
    ]
    assert _provider(init_integration).session_count == 0
    assert f"Session {SESSION_ID} state: ICE connected, DTLS connected" in caplog.text
    assert "warning audio_codec_unsupported: no AAC" in caplog.text
    assert f"Session {SESSION_ID} closed: peer_closed" in caplog.text
    assert "secret-password" not in caplog.text
    assert "192.0.2.10" not in caplog.text


async def test_session_closed_with_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A session the daemon ends with an error code ends in the browser."""
    send_message, subscription = await _offer(camera, mock_clients)
    assert subscription is not None
    subscription.deliver(
        SessionEventClosed(code="source_auth_failed", message="401 Unauthorized")
    )
    await run_tasks(hass)
    send_message.assert_called_once_with(
        WebRTCError("source_auth_failed", "401 Unauthorized")
    )
    assert _provider(init_integration).session_count == 0


@pytest.mark.parametrize(
    ("orientation", "expected"),
    [
        (Orientation.NO_TRANSFORM, "no_transform"),
        (Orientation.MIRROR, "mirror"),
        (Orientation.ROTATE_180, "rotate_180"),
        (Orientation.FLIP, "flip"),
        (Orientation.ROTATE_LEFT_AND_FLIP, "rotate_left_and_flip"),
        (Orientation.ROTATE_LEFT, "rotate_left"),
        (Orientation.ROTATE_RIGHT_AND_FLIP, "rotate_right_and_flip"),
        (Orientation.ROTATE_RIGHT, "rotate_right"),
    ],
)
async def test_orientation(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    orientation: Orientation,
    expected: str,
) -> None:
    """Each orientation preference is put as the same transform."""
    ws_client = await hass_ws_client(hass)
    await ws_client.send_json_auto_id(
        {
            "type": "camera/update_prefs",
            "entity_id": camera.entity_id,
            "orientation": orientation,
        }
    )
    assert (await ws_client.receive_json())["success"]
    # Neither viewed nor preloaded: the preference alone puts nothing.
    mock_clients.last.stream.put.assert_not_awaited()

    await _offer(camera, mock_clients)
    assert mock_clients.last.stream.put.call_args.kwargs["orientation"] == expected


async def test_prefs_update(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A preference change puts a viewed stream again, and a preloaded one."""
    client = mock_clients.last
    ws_client = await hass_ws_client(hass)
    await ws_client.send_json_auto_id(
        {
            "type": "camera/update_prefs",
            "entity_id": camera.entity_id,
            "preload_stream": True,
        }
    )
    assert (await ws_client.receive_json())["success"]
    client.stream.put.assert_awaited_once()
    assert client.stream.put.call_args.kwargs["preload"] is True

    await ws_client.send_json_auto_id(
        {
            "type": "camera/update_prefs",
            "entity_id": camera.entity_id,
            "preload_stream": False,
            "orientation": Orientation.ROTATE_180,
        }
    )
    assert (await ws_client.receive_json())["success"]
    assert client.stream.put.await_count == 2
    assert client.stream.put.call_args.kwargs["preload"] is False
    assert client.stream.put.call_args.kwargs["orientation"] == "rotate_180"


async def test_register_preloaded_camera(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A camera whose stream is preloaded is put when lotse serves it."""
    provider = _provider(init_integration)
    client = mock_clients.last
    await provider.async_register_camera(camera)
    client.stream.put.assert_not_awaited()

    ws_client = await hass_ws_client(hass)
    await ws_client.send_json_auto_id(
        {
            "type": "camera/update_prefs",
            "entity_id": camera.entity_id,
            "preload_stream": True,
        }
    )
    assert (await ws_client.receive_json())["success"]
    client.stream.put.reset_mock()
    await provider.async_register_camera(camera)
    client.stream.put.assert_awaited_once()
    assert client.stream.put.call_args.kwargs["preload"] is True


async def test_put_failures_logged(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A put outside an offer that fails is logged."""
    caplog.set_level(logging.DEBUG)
    provider = _provider(init_integration)
    client = mock_clients.last
    await _offer(camera, mock_clients)

    client.stream.put.side_effect = CommandError(
        "source_in_use", "another stream reads this source", {}
    )
    await provider.async_on_camera_prefs_update(camera)
    assert f"Could not put stream {STREAM_ID}: source_in_use" in caplog.text

    camera.set_stream_source(None)
    await provider.async_on_camera_prefs_update(camera)
    assert f"Camera of stream {STREAM_ID} has no stream source" in caplog.text

    camera.set_stream_source("webrtc://192.0.2.10/stream")
    await provider.async_on_camera_prefs_update(camera)
    assert (
        f"Camera of stream {STREAM_ID} has a stream source lotse does not read"
        in caplog.text
    )


async def test_unregister_camera(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A camera lotse no longer serves has its stream deleted."""
    provider = _provider(init_integration)
    client = mock_clients.last
    await _offer(camera, mock_clients)

    await provider.async_unregister_camera(camera)
    client.stream.delete.assert_awaited_once_with(stream_id=STREAM_ID)

    client.stream.delete.side_effect = ConnectionClosedError("not connected")
    await provider.async_unregister_camera(camera)
    assert f"Could not delete stream {STREAM_ID}: not connected" in caplog.text

    # Not put again after a reconnect.
    _drop(client)
    await run_tasks(hass)
    mock_clients.last.stream.put.assert_not_awaited()


async def test_offer_command_error(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A refused put or offer ends the session in the browser with its code."""
    client = mock_clients.last
    client.stream.put.side_effect = CommandError(
        "source_in_use", "another stream reads this source", {}
    )
    send_message, _ = await _offer(camera, mock_clients)
    send_message.assert_called_once_with(
        WebRTCError("source_in_use", "another stream reads this source")
    )
    client.webrtc.offer.assert_not_awaited()

    client.stream.put.side_effect = None
    client.webrtc.offer.side_effect = CommandError(
        "limit_reached", "too many sessions", {}
    )
    send_message, _ = await _offer(camera, mock_clients)
    send_message.assert_called_once_with(
        WebRTCError("limit_reached", "too many sessions")
    )
    assert _provider(init_integration).session_count == 0


async def test_offer_connection_lost(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """An offer whose connection drops on the way fails."""
    mock_clients.last.webrtc.offer.side_effect = ConnectionClosedError("closed")
    send_message, _ = await _offer(camera, mock_clients)
    send_message.assert_called_once_with(WebRTCError(ERROR_UNAVAILABLE, "closed"))


async def test_offer_without_stream_source(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A camera that lost its stream source fails the offer."""
    camera.set_stream_source(None)
    send_message, _ = await _offer(camera, mock_clients)
    send_message.assert_called_once_with(
        WebRTCError(ERROR_NO_STREAM_SOURCE, "The camera has no stream source")
    )


async def test_offer_unsupported_stream_source(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A camera whose stream source lotse does not read fails the offer."""
    camera.set_stream_source("webrtc://192.0.2.10/stream")
    send_message, _ = await _offer(camera, mock_clients)
    send_message.assert_called_once_with(
        WebRTCError(
            ERROR_UNSUPPORTED_STREAM_SOURCE,
            "lotse does not read the camera's stream source",
        )
    )
    mock_clients.last.stream.put.assert_not_awaited()


async def test_offer_while_disconnected(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """An offer while the daemon cannot be reached fails at once."""
    mock_clients.connect_errors = [CannotConnectError("refused")]
    _drop(mock_clients.last)
    await run_tasks(hass)
    assert init_integration.runtime_data.connection.client is None

    send_message, _ = await _offer(camera, mock_clients)
    send_message.assert_called_once_with(
        WebRTCError(ERROR_UNAVAILABLE, "lotse is not connected")
    )
    assert _provider(init_integration).session_count == 0


async def test_candidates(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Browser candidates are passed on, and failures only logged."""
    caplog.set_level(logging.DEBUG)
    client = mock_clients.last
    await _offer(camera, mock_clients)

    await camera.async_on_webrtc_candidate(
        SESSION_ID, RTCIceCandidateInit(CANDIDATE, sdp_mid="0", sdp_m_line_index=0)
    )
    client.webrtc.candidate.assert_awaited_once_with(
        session_id=SESSION_ID, candidate=CANDIDATE, sdp_mid="0", sdp_mline_index=0
    )

    client.webrtc.candidate.side_effect = CommandError(
        "session_not_found", "no such session", {}
    )
    await camera.async_on_webrtc_candidate(SESSION_ID, RTCIceCandidateInit(""))
    assert f"Candidate for session {SESSION_ID} not sent" in caplog.text

    client.webrtc.candidate.reset_mock()
    await camera.async_on_webrtc_candidate("unknown", RTCIceCandidateInit(CANDIDATE))
    client.webrtc.candidate.assert_not_awaited()
    assert "Ignoring a candidate for unknown session unknown" in caplog.text


async def test_candidates_before_the_offer_is_answered(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Candidates that arrive before the session is open are sent after the offer."""
    caplog.set_level(logging.DEBUG)
    client = mock_clients.last
    release = asyncio.Event()
    offer = client.webrtc.offer.side_effect

    async def _slow_offer(**kwargs: Any) -> Subscription[SessionEvent]:
        await release.wait()
        return offer(**kwargs)

    client.webrtc.offer.side_effect = _slow_offer
    task = asyncio.create_task(_offer(camera, mock_clients))
    await run_tasks(hass)
    for index in range(MAX_PENDING_CANDIDATES + 1):
        await camera.async_on_webrtc_candidate(
            SESSION_ID, RTCIceCandidateInit(f"{CANDIDATE} {index}")
        )
    client.webrtc.candidate.assert_not_awaited()
    assert f"Dropping a candidate for session {SESSION_ID}" in caplog.text

    release.set()
    await task
    await run_tasks(hass)
    assert client.webrtc.candidate.await_count == MAX_PENDING_CANDIDATES
    assert client.webrtc.candidate.await_args_list[0] == call(
        session_id=SESSION_ID,
        candidate=f"{CANDIDATE} 0",
        sdp_mid=None,
        sdp_mline_index=0,
    )


async def test_close_session(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """Closing a session unsubscribes, which ends it on the daemon."""
    client = mock_clients.last
    send_message, subscription = await _offer(camera, mock_clients)
    assert subscription is not None

    camera.close_webrtc_session(SESSION_ID)
    await run_tasks(hass)
    client.unsubscribe_id.assert_awaited_once_with(subscription.id)
    assert _provider(init_integration).session_count == 0

    # Closing it again, or an unknown session, does nothing.
    camera.close_webrtc_session(SESSION_ID)
    await run_tasks(hass)
    client.unsubscribe_id.assert_awaited_once()
    send_message.assert_not_called()


async def test_closed_by_daemon_and_browser(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A session the browser closed gets no error for the daemon closing it too."""
    send_message, subscription = await _offer(camera, mock_clients)
    assert subscription is not None
    subscription.deliver(SessionEventClosed(code="stream_deleted", message="gone"))
    camera.close_webrtc_session(SESSION_ID)
    await run_tasks(hass)
    # The subscription already ended, so there is nothing to unsubscribe.
    mock_clients.last.unsubscribe_id.assert_not_awaited()
    send_message.assert_not_called()


async def test_close_session_fails(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing unsubscribe is only logged."""
    caplog.set_level(logging.DEBUG)
    mock_clients.last.unsubscribe_id.side_effect = CommandError(
        "subscription_not_found", "no such subscription", {}
    )
    await _offer(camera, mock_clients)
    camera.close_webrtc_session(SESSION_ID)
    await run_tasks(hass)
    assert f"Unsubscribing session {SESSION_ID} failed" in caplog.text


async def test_close_session_while_offering(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A session closed before its offer is answered is ended once it is."""
    client = mock_clients.last
    release = asyncio.Event()
    offer = client.webrtc.offer.side_effect

    async def _slow_offer(**kwargs: Any) -> Subscription[SessionEvent]:
        await release.wait()
        return offer(**kwargs)

    client.webrtc.offer.side_effect = _slow_offer
    task = asyncio.create_task(_offer(camera, mock_clients))
    await run_tasks(hass)
    camera.close_webrtc_session(SESSION_ID)
    release.set()
    send_message, subscription = await task
    await run_tasks(hass)
    assert subscription is not None
    client.unsubscribe_id.assert_awaited_once_with(subscription.id)
    send_message.assert_not_called()


async def test_close_session_while_failing(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A session closed before its offer failed gets no error."""
    client = mock_clients.last
    release = asyncio.Event()

    async def _slow_failure(**kwargs: Any) -> None:
        await release.wait()
        raise CommandError("limit_reached", "too many sessions", {})

    client.webrtc.offer.side_effect = _slow_failure
    task = asyncio.create_task(_offer(camera, mock_clients))
    await run_tasks(hass)
    camera.close_webrtc_session(SESSION_ID)
    release.set()
    send_message, _ = await task
    send_message.assert_not_called()


async def test_reconnect_adopts_sessions(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a reconnect the sessions are adopted and their events relayed on."""
    caplog.set_level(logging.INFO)
    old = mock_clients.last
    send_message, subscription = await _offer(camera, mock_clients)
    assert subscription is not None
    subscription.deliver(SessionEventAnswer(sdp=ANSWER_SDP))
    await run_tasks(hass)

    _drop(old)
    await run_tasks(hass)
    new = mock_clients.last
    assert new is not old
    new.session.adopt.assert_awaited_once_with(session_id=SESSION_ID)
    assert f"Adopted session {SESSION_ID}" in caplog.text
    # Its stream is put again on the new connection.
    new.stream.put.assert_awaited_once()
    assert new.stream.put.call_args.kwargs["stream_id"] == STREAM_ID

    adopted = new.subscriptions[-1]
    adopted.deliver(SessionEventState(ice="connected", dtls="connected"))
    adopted.deliver(SessionEventCandidate(candidate=""))
    await run_tasks(hass)
    assert send_message.call_args_list == [
        call(WebRTCAnswer(ANSWER_SDP)),
        call(WebRTCCandidate(RTCIceCandidateInit(""))),
    ]

    # Browser candidates go to the new connection, closing unsubscribes there.
    await camera.async_on_webrtc_candidate(SESSION_ID, RTCIceCandidateInit(CANDIDATE))
    new.webrtc.candidate.assert_awaited_once()
    old.webrtc.candidate.assert_not_awaited()
    camera.close_webrtc_session(SESSION_ID)
    await run_tasks(hass)
    new.unsubscribe_id.assert_awaited_once_with(adopted.id)


async def test_reconnect_session_not_adopted(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A session the daemon no longer has ends in the browser."""
    caplog.set_level(logging.INFO)
    send_message, _ = await _offer(camera, mock_clients)
    mock_clients.configure = [
        lambda client: setattr(
            client.session.adopt,
            "side_effect",
            CommandError("session_not_found", "grace expired", {}),
        )
    ]
    _drop(mock_clients.last)
    await run_tasks(hass)
    send_message.assert_called_once_with(
        WebRTCError("session_not_found", "grace expired")
    )
    assert f"Session {SESSION_ID} could not be adopted: session_not_found" in (
        caplog.text
    )
    assert _provider(init_integration).session_count == 0


async def test_reconnect_lost_again_while_adopting(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A session whose adoption is cut short is adopted after the next reconnect."""
    send_message, _ = await _offer(camera, mock_clients)
    mock_clients.configure = [
        lambda client: setattr(
            client.session.adopt, "side_effect", ConnectionClosedError("closed")
        )
    ]
    _drop(mock_clients.last)
    await run_tasks(hass)
    assert _provider(init_integration).session_count == 1

    _drop(mock_clients.last)
    await run_tasks(hass)
    mock_clients.last.session.adopt.assert_awaited_once_with(session_id=SESSION_ID)
    send_message.assert_not_called()


async def test_candidates_while_disconnected(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Candidates of a session waiting for adoption are sent once it is adopted."""
    await _offer(camera, mock_clients)
    mock_clients.connect_errors = [CannotConnectError("refused")]
    _drop(mock_clients.last)
    await run_tasks(hass)
    await camera.async_on_webrtc_candidate(SESSION_ID, RTCIceCandidateInit(CANDIDATE))

    await _tick(hass, freezer, 1)
    assert init_integration.runtime_data.connection.client is mock_clients.last
    mock_clients.last.webrtc.candidate.assert_awaited_once_with(
        session_id=SESSION_ID, candidate=CANDIDATE, sdp_mid=None, sdp_mline_index=0
    )


async def test_close_session_while_adopting(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A session closed while it is being adopted is closed on the daemon."""
    caplog.set_level(logging.DEBUG)
    await _offer(camera, mock_clients)
    release = asyncio.Event()

    def _slow_adopt(client: MagicMock) -> None:
        adopt = client.session.adopt.side_effect

        async def _adopt(**kwargs: Any) -> Subscription[SessionEvent]:
            await release.wait()
            return adopt(**kwargs)

        client.session.adopt.side_effect = _adopt
        client.session.close.side_effect = CommandError(
            "session_not_found", "no such session", {}
        )

    mock_clients.configure = [_slow_adopt]
    _drop(mock_clients.last)
    await run_tasks(hass)
    new = mock_clients.last
    camera.close_webrtc_session(SESSION_ID)
    await run_tasks(hass)
    new.session.close.assert_awaited_once_with(session_id=SESSION_ID)
    assert f"Closing session {SESSION_ID} failed: session_not_found" in caplog.text

    release.set()
    await run_tasks(hass)
    new.unsubscribe_id.assert_awaited_once_with(new.subscriptions[-1].id)


async def test_close_session_while_disconnected(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """A session closed while the daemon is unreachable is left to its grace period."""
    old = mock_clients.last
    await _offer(camera, mock_clients)
    mock_clients.connect_errors = [CannotConnectError("refused")]
    _drop(old)
    await run_tasks(hass)
    camera.close_webrtc_session(SESSION_ID)
    await run_tasks(hass)
    old.unsubscribe_id.assert_not_awaited()
    assert _provider(init_integration).session_count == 0


async def test_put_while_disconnected(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A preloaded stream put while the daemon is unreachable is put on reconnect."""
    mock_clients.connect_errors = [CannotConnectError("refused")]
    _drop(mock_clients.last)
    await run_tasks(hass)
    await _provider(init_integration).async_unregister_camera(camera)

    ws_client = await hass_ws_client(hass)
    await ws_client.send_json_auto_id(
        {
            "type": "camera/update_prefs",
            "entity_id": camera.entity_id,
            "preload_stream": True,
        }
    )
    assert (await ws_client.receive_json())["success"]

    await _tick(hass, freezer, 1)
    mock_clients.last.stream.put.assert_awaited_once()
    assert mock_clients.last.stream.put.call_args.kwargs["preload"] is True


async def test_reload_adopts_sessions(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    camera: MockCamera,
    mock_clients: MockClients,
) -> None:
    """Reloading the entry hands its sessions to the next one."""
    send_message, _ = await _offer(camera, mock_clients)
    assert await hass.config_entries.async_reload(init_integration.entry_id)
    await run_tasks(hass)

    client = mock_clients.last
    client.session.adopt.assert_awaited_once_with(session_id=SESSION_ID)
    assert camera.webrtc_provider is _provider(init_integration)
    client.subscriptions[-1].deliver(SessionEventCandidate(candidate=""))
    await run_tasks(hass)
    send_message.assert_called_once_with(WebRTCCandidate(RTCIceCandidateInit("")))


async def test_reload_spawned_does_not_hand_over(
    hass: HomeAssistant,
    mock_spawn_entry: MockConfigEntry,
    mock_server: MagicMock,
    mock_clients: MockClients,
) -> None:
    """A daemon Home Assistant stops on unload leaves no sessions to adopt."""
    mock_spawn_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_spawn_entry.entry_id)
    camera = MockCamera("camera_unique_id")
    await async_setup_camera(hass, camera)
    await _offer(camera, mock_clients)
    assert _provider(mock_spawn_entry).session_count == 1

    assert await hass.config_entries.async_reload(mock_spawn_entry.entry_id)
    await run_tasks(hass)
    mock_clients.last.session.adopt.assert_not_awaited()
    assert _provider(mock_spawn_entry).session_count == 0


def test_ice_servers() -> None:
    """The browser's ICE servers are passed on with their credentials."""
    camera = Mock()
    camera.async_get_webrtc_client_configuration.return_value = (
        WebRTCClientConfiguration(
            configuration=RTCConfiguration(
                ice_servers=[
                    RTCIceServer(urls="stun:stun.example.com:3478"),
                    RTCIceServer(
                        urls=["turn:turn.example.com:3478", "turns:turn.example.com"],
                        username="user",
                        credential="pass",
                    ),
                ]
            )
        )
    )
    assert ice_servers_for(camera) == [
        IceServer(urls=["stun:stun.example.com:3478"]),
        IceServer(
            urls=["turn:turn.example.com:3478", "turns:turn.example.com"],
            username="user",
            credential="pass",
        ),
    ]


@pytest.mark.parametrize(
    ("entity_id", "expected"),
    [
        ("camera.front_door", "camera.front_door"),
        ("camera." + "a" * 121, "camera." + "a" * 121),
    ],
)
def test_stream_id(entity_id: str, expected: str) -> None:
    """A camera's stream id is its entity id."""
    assert stream_id_for(entity_id) == expected


@pytest.mark.parametrize("entity_id", ["camera." + "a" * 200, "camera.tür"])
def test_stream_id_shortened(entity_id: str) -> None:
    """An entity id lotse does not take as a stream id is shortened or replaced."""
    stream_id = stream_id_for(entity_id)
    assert len(stream_id) <= MAX_STREAM_ID
    assert stream_id.startswith("camera.")
    assert all(c.isascii() and (c.isalnum() or c in "._-") for c in stream_id)
    assert stream_id == stream_id_for(entity_id)
    assert stream_id != stream_id_for(entity_id + "b")
