"""lotse as the cameras' WebRTC provider.

Each camera is one lotse stream, whose id is the camera's entity id. Before
an offer the stream is put again (`stream/put` is idempotent) with the
camera's current source and preferences. Each browser session is one
`webrtc/offer` subscription under Home Assistant's own session id; its
events are relayed to the session's `send_message`. When the connection is
lost the daemon keeps the sessions for its grace period, and after the
reconnect they are adopted again (`session/adopt`).
"""

import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
import re
from typing import Final, override

from lotse_client import CommandError, LotseClient, LotseError, Subscription
from lotse_client.models import (
    IceServer,
    Orientation as LotseOrientation,
    SessionEvent,
    SessionEventAnswer,
    SessionEventCandidate,
    SessionEventClosed,
    SessionEventSession,
    SessionEventState,
    SessionEventWarning,
    SourceSpec,
)
from webrtc_models import RTCIceCandidateInit

from homeassistant.components.camera import (
    Camera,
    CameraWebRTCProvider,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
    get_dynamic_camera_stream_settings,
)
from homeassistant.components.stream import Orientation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .connection import LotseConnection
from .const import DOMAIN, ERROR_NO_STREAM_SOURCE, ERROR_UNAVAILABLE

_LOGGER = logging.getLogger(__name__)

ORIENTATIONS: Final[dict[Orientation, LotseOrientation]] = {
    Orientation.NO_TRANSFORM: "no_transform",
    Orientation.MIRROR: "mirror",
    Orientation.ROTATE_180: "rotate_180",
    Orientation.FLIP: "flip",
    Orientation.ROTATE_LEFT_AND_FLIP: "rotate_left_and_flip",
    Orientation.ROTATE_LEFT: "rotate_left",
    Orientation.ROTATE_RIGHT_AND_FLIP: "rotate_right_and_flip",
    Orientation.ROTATE_RIGHT: "rotate_right",
}
"""Home Assistant's orientation preference, 1 to 8 as the EXIF orientation tag
numbers them, and the same transform in lotse's `stream/put`."""

NORMAL_CLOSE_CODES: Final = frozenset({"peer_closed", "session_closed"})
"""The `closed` codes of a session that ended without a fault."""

MAX_STREAM_ID: Final = 128
"""The longest stream id lotse accepts."""
_STREAM_ID: Final = re.compile(r"[A-Za-z0-9._-]+")
MAX_PENDING_CANDIDATES: Final = 32
"""Browser candidates kept for a session that has no subscription yet."""


class _NoStreamSourceError(Exception):
    """The camera has no stream source."""


@dataclass(frozen=True, slots=True)
class _Handover:
    """A live session passed from an unloaded entry to the next one."""

    stream_id: str
    send_message: WebRTCSendMessage


DATA_HANDOVER: HassKey[dict[str, _Handover]] = HassKey(f"{DOMAIN}_handover")
"""Live sessions an unloaded entry left for the next one to adopt."""


@dataclass(slots=True)
class _Session:
    """One browser session and the subscription that relays its events."""

    session_id: str
    stream_id: str
    send_message: WebRTCSendMessage
    client: LotseClient | None = None
    """The connection the subscription is on; `None` before it opened."""
    subscription: Subscription[SessionEvent] | None = None
    task: asyncio.Task[None] | None = None
    pending: list[RTCIceCandidateInit] = field(default_factory=list)
    """Browser candidates that arrived while the session had no subscription."""
    opening: bool = False
    """Its `webrtc/offer` is on its way; a reconnect does not adopt it."""
    closed: bool = False
    """Home Assistant closed the session."""


def stream_id_for(camera: Camera) -> str:
    """The lotse stream id of a camera: its entity id, shortened if too long."""
    entity_id = camera.entity_id
    if len(entity_id) <= MAX_STREAM_ID and _STREAM_ID.fullmatch(entity_id):
        return entity_id
    digest = hashlib.sha256(entity_id.encode()).hexdigest()[:16]
    prefix = re.sub(r"[^A-Za-z0-9._-]", "_", entity_id)[: MAX_STREAM_ID - 17]
    return f"{prefix}-{digest}"


def ice_servers_for(camera: Camera) -> list[IceServer]:
    """The ICE servers the camera's browser got, as lotse takes them."""
    configuration = camera.async_get_webrtc_client_configuration().configuration
    return [
        IceServer(
            urls=[server.urls] if isinstance(server.urls, str) else list(server.urls),
            username=server.username,
            credential=server.credential,
        )
        for server in configuration.ice_servers
    ]


class LotseWebRTCProvider(CameraWebRTCProvider):
    """Serve the cameras' WebRTC sessions through lotse."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, connection: LotseConnection
    ) -> None:
        """Initialize the provider."""
        self._hass = hass
        self._entry = entry
        self._connection = connection
        self._sessions: dict[str, _Session] = {}
        self._cameras: dict[str, Camera] = {}
        """The cameras whose stream was put, by stream id."""

    @property
    @override
    def domain(self) -> str:
        """Return the integration domain of the provider."""
        return DOMAIN

    @property
    def session_count(self) -> int:
        """The browser sessions the provider relays."""
        return len(self._sessions)

    @callback
    @override
    def async_is_supported(self, stream_source: str) -> bool:
        """Whether the daemon reads the stream source's URL scheme."""
        if (info := self._connection.info) is None:
            return False
        return stream_source.partition(":")[0].lower() in info.schemes

    @override
    async def async_handle_async_webrtc_offer(
        self,
        camera: Camera,
        offer_sdp: str,
        session_id: str,
        send_message: WebRTCSendMessage,
    ) -> None:
        """Put the camera's stream and open a session for the browser's offer."""
        stream_id = stream_id_for(camera)
        session = _Session(session_id, stream_id, send_message, opening=True)
        self._sessions[session_id] = session
        if (client := self._connection.client) is None:
            self._async_fail(session, ERROR_UNAVAILABLE, "lotse is not connected")
            return
        try:
            await self._async_put_stream(client, camera)
            subscription = await client.webrtc.offer(
                stream_id=stream_id,
                sdp=offer_sdp,
                session_id=session_id,
                ice_servers=ice_servers_for(camera),
            )
        except _NoStreamSourceError:
            self._async_fail(
                session, ERROR_NO_STREAM_SOURCE, "The camera has no stream source"
            )
            return
        except CommandError as err:
            self._async_fail(session, err.code, err.message)
            return
        except LotseError as err:
            self._async_fail(session, ERROR_UNAVAILABLE, str(err))
            return
        _LOGGER.debug("Session %s opened on stream %s", session_id, stream_id)
        self._async_attach(session, client, subscription)

    @override
    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Pass a browser candidate on, or keep it until the session is attached."""
        if (session := self._sessions.get(session_id)) is None:
            _LOGGER.debug("Ignoring a candidate for unknown session %s", session_id)
            return
        client = self._connection.client
        if client is None or session.client is not client:
            if len(session.pending) < MAX_PENDING_CANDIDATES:
                session.pending.append(candidate)
            else:
                _LOGGER.debug("Dropping a candidate for session %s", session_id)
            return
        await self._async_send_candidate(client, session_id, candidate)

    @callback
    @override
    def async_close_session(self, session_id: str) -> None:
        """End a session on the daemon; never raises."""
        if (session := self._sessions.pop(session_id, None)) is None:
            return
        session.closed = True
        _LOGGER.debug("Closing session %s", session_id)
        if (
            session.subscription is not None
            and session.client is not None
            and session.client is self._connection.client
        ):
            self._entry.async_create_background_task(
                self._hass,
                self._async_unsubscribe(session_id, session.subscription),
                f"{DOMAIN} close session",
            )

    @override
    async def async_register_camera(self, camera: Camera) -> None:
        """Put the stream of a camera whose stream is preloaded."""
        settings = await get_dynamic_camera_stream_settings(
            self._hass, camera.entity_id
        )
        if settings.preload_stream:
            await self._async_put_camera(camera)

    @override
    async def async_unregister_camera(self, camera: Camera) -> None:
        """Delete the camera's stream; its sessions end with it."""
        stream_id = stream_id_for(camera)
        self._cameras.pop(stream_id, None)
        if (client := self._connection.client) is None:
            return
        try:
            await client.stream.delete(stream_id=stream_id)
        except LotseError as err:
            _LOGGER.warning("Could not delete stream %s: %s", stream_id, err)
        else:
            _LOGGER.debug("Deleted stream %s", stream_id)

    @override
    async def async_on_camera_prefs_update(self, camera: Camera) -> None:
        """Put the stream again with the new preferences."""
        settings = await get_dynamic_camera_stream_settings(
            self._hass, camera.entity_id
        )
        if stream_id_for(camera) in self._cameras or settings.preload_stream:
            await self._async_put_camera(camera)

    async def async_on_connected(self, client: LotseClient) -> None:
        """Adopt the sessions of a lost connection, then put every stream again."""
        for session in list(self._sessions.values()):
            if session.client is not client and not session.opening:
                await self._async_adopt(client, session)
        for camera in list(self._cameras.values()):
            await self._async_put_camera(camera)

    @callback
    def async_take_handover(self) -> None:
        """Take the live sessions an unloaded entry left behind."""
        for session_id, handover in self._hass.data.pop(DATA_HANDOVER, {}).items():
            self._sessions[session_id] = _Session(
                session_id, handover.stream_id, handover.send_message
            )

    @callback
    def async_unload(self, *, handover: bool) -> None:
        """Forget every session; with `handover`, leave them for the next entry."""
        if handover and self._sessions:
            self._hass.data[DATA_HANDOVER] = {
                session_id: _Handover(session.stream_id, session.send_message)
                for session_id, session in self._sessions.items()
            }
            _LOGGER.debug("Leaving %s sessions to be adopted", len(self._sessions))
        for session in self._sessions.values():
            if session.task is not None:
                session.task.cancel()
        self._sessions.clear()

    async def _async_put_stream(self, client: LotseClient, camera: Camera) -> None:
        """Put the camera's stream with its current source and preferences."""
        if not (source := await camera.stream_source()):
            raise _NoStreamSourceError
        settings = await get_dynamic_camera_stream_settings(
            self._hass, camera.entity_id
        )
        stream_id = stream_id_for(camera)
        result = await client.stream.put(
            stream_id=stream_id,
            sources=[SourceSpec(url=source)],
            orientation=ORIENTATIONS[settings.orientation],
            preload=settings.preload_stream,
        )
        self._cameras[stream_id] = camera
        _LOGGER.debug(
            "Put stream %s (created: %s, preload: %s, orientation: %s)",
            stream_id,
            result.created,
            settings.preload_stream,
            settings.orientation.name,
        )

    async def _async_put_camera(self, camera: Camera) -> None:
        """Put the camera's stream outside an offer, logging what fails."""
        stream_id = stream_id_for(camera)
        if (client := self._connection.client) is None:
            # Put once the connection is back.
            self._cameras[stream_id] = camera
            return
        try:
            await self._async_put_stream(client, camera)
        except _NoStreamSourceError:
            _LOGGER.debug("Camera of stream %s has no stream source", stream_id)
        except LotseError as err:
            _LOGGER.warning("Could not put stream %s: %s", stream_id, err)

    async def _async_adopt(self, client: LotseClient, session: _Session) -> None:
        """Take a session back after a reconnect, or end it in the browser."""
        if session.task is not None:
            session.task.cancel()
            session.task = None
        session.client = None
        session.subscription = None
        try:
            subscription = await client.session.adopt(session_id=session.session_id)
        except CommandError as err:
            _LOGGER.info(
                "Session %s could not be adopted: %s", session.session_id, err.code
            )
            self._async_fail(session, err.code, err.message)
            return
        except LotseError as err:
            _LOGGER.debug("Session %s not adopted: %s", session.session_id, err)
            return
        _LOGGER.info("Adopted session %s", session.session_id)
        self._async_attach(session, client, subscription)

    @callback
    def _async_attach(
        self,
        session: _Session,
        client: LotseClient,
        subscription: Subscription[SessionEvent],
    ) -> None:
        """Relay a session's subscription and pass on the candidates it kept."""
        session.opening = False
        if session.closed:
            # Home Assistant closed the session while it was being opened.
            self._entry.async_create_background_task(
                self._hass,
                self._async_unsubscribe(session.session_id, subscription),
                f"{DOMAIN} close session",
            )
            return
        session.client = client
        session.subscription = subscription
        session.task = self._entry.async_create_background_task(
            self._hass,
            self._async_relay(session, subscription),
            f"{DOMAIN} session {session.session_id}",
        )
        pending, session.pending = session.pending, []
        for candidate in pending:
            self._entry.async_create_background_task(
                self._hass,
                self._async_send_candidate(client, session.session_id, candidate),
                f"{DOMAIN} candidate",
            )

    @callback
    def _async_fail(self, session: _Session, code: str, message: str) -> None:
        """End a session that could not be opened or adopted."""
        session.opening = False
        if self._sessions.get(session.session_id) is session:
            del self._sessions[session.session_id]
        _LOGGER.debug("Session %s failed: %s", session.session_id, code)
        if not session.closed:
            session.send_message(WebRTCError(code, message))

    async def _async_relay(
        self, session: _Session, subscription: Subscription[SessionEvent]
    ) -> None:
        """Relay a session's events until it closes or the connection is lost."""
        try:
            async for event in subscription:
                self._async_handle_event(session, event)
        except LotseError as err:
            _LOGGER.debug("Session %s lost its connection: %s", session.session_id, err)

    @callback
    def _async_handle_event(self, session: _Session, event: SessionEvent) -> None:
        """Pass one session event on to the browser."""
        match event:
            case SessionEventAnswer():
                session.send_message(WebRTCAnswer(event.sdp))
            case SessionEventCandidate():
                session.send_message(
                    WebRTCCandidate(
                        RTCIceCandidateInit(
                            event.candidate,
                            sdp_mid=event.sdp_mid,
                            sdp_m_line_index=event.sdp_mline_index,
                        )
                    )
                )
            case SessionEventClosed():
                if self._sessions.get(session.session_id) is session:
                    del self._sessions[session.session_id]
                if event.code in NORMAL_CLOSE_CODES:
                    _LOGGER.debug(
                        "Session %s closed: %s", session.session_id, event.code
                    )
                else:
                    _LOGGER.info(
                        "Session %s on stream %s closed: %s",
                        session.session_id,
                        session.stream_id,
                        event.code,
                    )
                    if not session.closed:
                        session.send_message(WebRTCError(event.code, event.message))
            case SessionEventWarning():
                _LOGGER.debug(
                    "Session %s warning %s: %s",
                    session.session_id,
                    event.code,
                    event.message,
                )
            case SessionEventState():
                _LOGGER.debug(
                    "Session %s state: ICE %s, DTLS %s",
                    session.session_id,
                    event.ice,
                    event.dtls,
                )
            case SessionEventSession():
                _LOGGER.debug("Session %s started", event.session_id)

    async def _async_send_candidate(
        self, client: LotseClient, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Send one browser candidate to the daemon; a failure is logged."""
        try:
            await client.webrtc.candidate(
                session_id=session_id,
                candidate=candidate.candidate,
                sdp_mid=candidate.sdp_mid,
                sdp_mline_index=candidate.sdp_m_line_index,
            )
        except LotseError as err:
            _LOGGER.debug("Candidate for session %s not sent: %s", session_id, err)

    async def _async_unsubscribe(
        self, session_id: str, subscription: Subscription[SessionEvent]
    ) -> None:
        """End a session's subscription, and so the session; never raises."""
        try:
            await subscription.unsubscribe()
        except LotseError as err:
            _LOGGER.debug("Unsubscribing session %s failed: %s", session_id, err)
