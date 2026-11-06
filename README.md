# lotse for Home Assistant

A Home Assistant integration that makes [lotse](https://github.com/edenhaus/lotse),
a media daemon for IP cameras, the WebRTC provider of your cameras: when a
camera's live view opens in Home Assistant, lotse reads the camera's stream
(RTSP and the other schemes the daemon reports) and sends it to the browser
over WebRTC.

The integration is written to Home Assistant core's standards so it can move
into core later. Until then it is a custom integration, installable with HACS.

## Use cases

- **Live view in the dashboard.** A camera card or the camera's dialog opens
  a WebRTC session through lotse: the browser plays the camera's stream with
  low latency, without Home Assistant transcoding it.
- **Viewing from outside your home.** Over Home Assistant Cloud, the browser
  gets TURN servers for its connection; Home Assistant passes the same ones to
  lotse, so the live view also works when the browser is not on your network.
- **Fast start for the cameras you open most.** Turn on *Preload stream* in
  a camera's settings: lotse keeps that camera connected without viewers, so
  a live view starts with a picture at once instead of after the camera's
  connection and its next keyframe.
- **A rotated or mirrored mount.** Set the camera's *Orientation*: lotse
  passes it on, and browsers that support it turn the picture.

## Requirements

- Home Assistant 2026.10 or newer.
- A lotse binary on the Home Assistant host (Linux), or a lotse that already
  runs and whose control socket Home Assistant may open.
- Cameras whose integration gives Home Assistant a stream source, such as
  Generic Camera, ONVIF or most vendor integrations.
- The Python client, `lotse-client`, is not on PyPI yet: Home Assistant
  installs it from a pinned commit of
  [lotse-clients](https://github.com/edenhaus/lotse-clients), which needs
  `git` in Home Assistant's environment.

## Installation

With HACS: add `https://github.com/edenhaus/lotse-ha` as a custom repository
of type *Integration*, install *lotse*, and restart Home Assistant.

By hand: copy `custom_components/lotse` into the `custom_components` folder of
your Home Assistant configuration, and restart Home Assistant.

## Configuration

Go to **Settings → Devices & services → Add integration** and pick *lotse*.
Choose how Home Assistant reaches the daemon:

- **Run lotse**: Home Assistant runs the daemon itself.
  - *lotse binary*: the path of the `lotse` executable; the form suggests the
    one on the `PATH`. Home Assistant checks that it is executable and that
    `lotse --version` names lotse.

  Each start gets a fresh private directory for the control socket
  (`lotse serve --socket <dir>/lotse.sock`). The daemon's log goes to Home
  Assistant's log at each line's own level. Home Assistant restarts the
  daemon when it exits or stops answering, and stops it (SIGTERM, then
  SIGKILL after 5 s) when Home Assistant stops or the integration is
  unloaded.
- **Connect to a running lotse**: the daemon runs on its own, for example as
  a service on the host.
  - *Control socket*: the path of its control socket, `/run/lotse/lotse.sock`
    by default. Home Assistant connects to check it before the entry is made.
    The daemon admits only the user it was started as, or the one its
    `--allow-uid` names.

Only one lotse entry can exist. To switch between the two, or change the
path, use **Reconfigure** on the entry.

## Examples

### Home Assistant runs lotse

With the `lotse` binary at `/usr/local/bin/lotse` on the Home Assistant host
(or in its container):

1. **Add integration → lotse → Run lotse**.
2. Keep the suggested *lotse binary* or enter `/usr/local/bin/lotse`, and
   submit. The entry is created once `lotse --version` names lotse; Home
   Assistant then starts the daemon and connects to it.

### lotse in its own container next to Home Assistant

lotse's release image (`ghcr.io/edenhaus/lotse`, published without a
`latest` tag: pin the release's tag or digest) serves its control socket at
`/run/lotse/lotse.sock`. Share that directory with Home Assistant through a
volume. Both containers run as root, the user lotse admits on its socket, and
lotse drops its privileges after binding. Host networking lets browsers reach
lotse's WebRTC port (UDP 18556) directly.

```yaml
services:
  lotse:
    image: ghcr.io/edenhaus/lotse:<release>
    network_mode: host
    restart: unless-stopped
    volumes:
      - lotse-socket:/run/lotse

  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:stable
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./config:/config
      - lotse-socket:/run/lotse

volumes:
  lotse-socket:
```

Then **Add integration → lotse → Connect to a running lotse**, and keep the
suggested *Control socket*, `/run/lotse/lotse.sock`.

## How it works

- Home Assistant holds one connection to the daemon's control socket and
  pings it every 10 s. A missing pong or a closed connection counts as lost:
  Home Assistant reconnects at once, then after 1, 2, 4, … up to 60 s, and
  restarts a daemon it runs. While the daemon cannot be reached, live views
  fail at once, so the frontend falls back to HLS.
- Each camera is one lotse stream, named after the camera's entity id. Before
  every live view Home Assistant sends the stream's definition again with the
  camera's current stream source, its *orientation* preference (passed to
  browsers that turn the picture themselves) and its *preload stream*
  preference (lotse keeps such a camera connected without viewers).
- Each browser session is one lotse session under Home Assistant's own
  session id, with the ICE servers the browser got, so TURN servers from Home
  Assistant Cloud reach lotse too. Answers, candidates and errors go straight
  back to the browser.
- When the connection is lost, the daemon keeps the sessions for 10 s.
  After the reconnect, and after a reload of the integration while lotse
  keeps running, Home Assistant adopts them again, so live views continue.
- Nothing is polled: the daemon pushes every session event.

## What works

- Live view over WebRTC for every camera whose stream source scheme lotse
  reports (`rtsp`, `rtsps`, …), with trickled ICE in both directions.
- Orientation and preload preferences.
- Running and supervising the daemon, or connecting to a running one.
- Diagnostics: the daemon's `info`, its counters and stream states, with
  addresses and credentials redacted.
- Repair issues when lotse runs with its sandbox off, or without one of its
  layers (privilege drop, `no_new_privs`, seccomp, Landlock filesystem
  rules). They are checked on every connection and withdrawn once fixed.

## Not supported yet

- Snapshots: the integration does not provide camera images; Home Assistant
  takes them from the camera's own integration or its stream as before.
- Recordings and two-way audio, which lotse supports, are not used yet.
- Repair issues for a stream that keeps failing; the daemon's errors reach
  the browser and the log.
- If another integration registers a WebRTC provider for the same URL
  schemes, Home Assistant picks one of the two per camera, in no defined
  order. Deterministic selection needs a change in Home Assistant.
- An options flow; there is nothing to set besides the connection.

## Troubleshooting

- The integration logs under `custom_components.lotse`, the daemon's own
  lines under `custom_components.lotse.daemon`. For more detail:

  ```yaml
  logger:
    logs:
      custom_components.lotse: debug
  ```

- **Download diagnostics** on the entry shows whether the daemon is
  connected, its version, limits, sandbox, counters and streams.
- A live view that fails shows lotse's error code in the browser's console
  (`source_auth_failed`, `source_unreachable`, `video_codec_unsupported`, …).
- Stream source URLs carry camera passwords: the integration never logs them,
  only stream ids.

## Removal

Delete the entry under **Settings → Devices & services → lotse**. A daemon
that Home Assistant runs is stopped; from a daemon that runs on its own, the
streams of Home Assistant's cameras are deleted, and the daemon keeps
running. Then remove the integration in HACS, or delete
`custom_components/lotse`.

## Development

[mise](https://mise.jdx.dev) installs the tools (`mise install`, which also
installs the git hooks), and uv the dependencies, pinned in `uv.lock`.

| Command | What it does |
|---|---|
| `mise run check` | every prek hook on all files: ruff, ruff format, mypy, file hygiene |
| `mise run test` | the tests with the 100 % coverage gate |

The tests use Home Assistant's own fixtures through
`pytest-homeassistant-custom-component`, with the client and the daemon
mocked. CI (`.github/workflows/ci.yml`) also runs hassfest and the HACS
validation.

## License

[Apache-2.0](LICENSE), as Home Assistant core.
