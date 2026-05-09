# SyncUDP Home Assistant Add-on

SyncUDP packages the SyncLyrics UDP-only variant as a Home Assistant add-on.
The add-on receives PCM/RTP audio over UDP, runs the existing recognition and
lyrics matching pipeline, and displays synchronized lyrics in the web UI.

## Installation

1. Add this repository as a Home Assistant add-on repository.
2. Install the **SyncLyrics (UDP Only)** add-on.
3. Configure the UDP port and optional Music Assistant / Last.fm settings.
4. Start the add-on and open `http://<home-assistant-host>:9012`.

The add-on uses host networking so the web UI defaults to port `9012`, HTTPS to
`9013`, and UDP audio input to `6056`.

## Supported input

SyncLyricsUDP is UDP-only. It does not expose Reaper, Spotify app control,
Spicetify, Windows media-session, browser microphone, line-in, desktop capture,
or general local audio-device capture as input sources.

## Persistent data

Runtime settings, caches, generated certificates, lyrics, album art, and logs are
stored in the Home Assistant add-on config mount (`/config`) and are not bundled
with this repository.
