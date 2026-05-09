from os import path
from typing import Any, Optional, List, Dict
import asyncio
import time
import random  # ADD THIS IMPORT
from functools import wraps

from quart import Quart, render_template, redirect, flash, request, jsonify, url_for, send_from_directory, websocket
from lyrics import get_timed_lyrics_previous_and_next, get_current_provider, _is_manually_instrumental, _is_cached_instrumental, set_manual_instrumental
import lyrics as lyrics_module
from system_utils import get_current_song_meta_data, get_album_db_folder, load_album_art_from_db, save_album_db_metadata, get_cached_art_path, cleanup_old_art, clear_artist_image_cache
from system_utils import state as system_state
from state_manager import *
from config import LYRICS, RESOURCES_DIR, ALBUM_ART_DB_DIR, SERVER, conf
from settings import settings
from logging_config import get_logger

# Import shared Spotify singleton for controls - ensures all stats are consolidated
from providers.spotify_api import get_shared_spotify_client

import os
from pathlib import Path
import json
import uuid

logger = get_logger(__name__)

# Cache version based on app start time for cache busting
APP_START_TIME = int(time.time())

# Add this global near other globals at the top of server.py
# Global cache for slideshow images
_slideshow_cache = {
    'images': [],
    'last_update': 0
}
_SLIDESHOW_CACHE_TTL = 3600  # 1 hour

# Global throttle for cover art logs (prevents spam when frontend makes multiple requests)
# Key: file path (str), Value: last log timestamp
_cover_art_log_throttle = {}

# Cache for instrumental markers (avoids disk read every /lyrics poll)
# Key: (artist, title), Value: list of marker timestamps
_instrumental_markers_cache = {
    'key': None,       # (artist, title) tuple
    'markers': []      # List of timestamps
}

# Legacy playback sources - these use existing Windows/Spotify routing.
# Plugin sources not in this set get routed to their own playback handlers.
LEGACY_PLAYBACK_SOURCES = {'audio_recognition'}

TEMPLATE_DIRECTORY = str(RESOURCES_DIR / "templates")
STATIC_DIRECTORY = str(RESOURCES_DIR)
app = Quart(__name__, template_folder=TEMPLATE_DIRECTORY, static_folder=STATIC_DIRECTORY)
app.config['SERVER_NAME'] = None
app.secret_key = SERVER.get("secret_key")

# --- Helper Functions ---

def get_spotify_client():
    """
    Helper to get the shared Spotify singleton client.
    
    This ensures all API calls across the app use the same instance,
    so statistics are accurately consolidated and caching is efficient.
    """
    client = get_shared_spotify_client()
    return client if client and client.initialized else None

@app.context_processor
async def inject_cache_version() -> dict:
    """Inject cache busting version into all templates"""
    return {"cache_version": APP_START_TIME}

@app.context_processor
async def theme() -> dict: 
    return {"theme": get_attribute_js_notation(get_state(), 'theme')}

@app.after_request
async def add_cache_headers(response):
    """
    Add Cache-Control headers to prevent stale content issues.
    - Static assets: 6min cache with ETag/Last-Modified for efficient revalidation
    - API/pages: no caching to ensure fresh data
    - Routes that set their own Cache-Control are respected (e.g., image serving)
    
    ETag and Last-Modified enable 304 Not Modified responses, so even after
    max-age expires, the browser only downloads new content if files changed.
    This fixes stale cache issues in Home Assistant iFrame while maintaining performance.
    """
    req_path = request.path
    
    # Media browser static assets (React build with content hashes - safe to cache forever)
    if req_path.startswith('/media-browser/static/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    # Static assets (CSS, JS, images, fonts)
    elif req_path.startswith('/static/'):
        # Reduced from 3600s (1hr) to 360s (6min) to ensure updates propagate faster
        # Combined with ETag/Last-Modified, this enables efficient revalidation
        response.headers['Cache-Control'] = 'public, max-age=360, must-revalidate'
        
        # Add ETag and Last-Modified for static files to enable 304 responses
        # This makes cache validation very efficient even with shorter max-age
        try:
            # Resolve the actual file path from the request URL
            # /static/js/main.js -> STATIC_DIRECTORY/js/main.js
            relative_path = req_path[len('/static/'):]  # Remove '/static/' prefix
            file_path = os.path.join(STATIC_DIRECTORY, relative_path)
            
            if os.path.isfile(file_path):
                # Last-Modified: file modification timestamp
                mtime = os.path.getmtime(file_path)
                from email.utils import formatdate
                response.headers['Last-Modified'] = formatdate(mtime, usegmt=True)
                
                # ETag: hash of file path + mtime (fast, no file read needed)
                # Using mtime ensures ETag changes when file is modified
                import hashlib
                etag_source = f"{file_path}:{mtime}".encode('utf-8')
                etag = hashlib.md5(etag_source).hexdigest()
                response.headers['ETag'] = f'"{etag}"'
        except Exception:
            # If file path resolution fails, skip ETag/Last-Modified
            # The response will still work, just without validation headers
            pass
    # API endpoints and pages - no caching (unless route already set its own)
    elif req_path.startswith('/api/') or req_path in ['/', '/lyrics', '/current-track', '/config', '/settings']:
        # Don't overwrite if route already set cache headers (e.g., image serving routes)
        if 'Cache-Control' not in response.headers:
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
    
    return response

# --- Font Files Route ---
# Explicit route for serving font files (Quart's static folder doesn't always pick up new directories)

@app.route('/fonts/custom.css')
async def serve_custom_fonts_css():
    """Dynamically generate CSS for custom fonts."""
    from font_scanner import generate_custom_css
    css = generate_custom_css(RESOURCES_DIR / "fonts")
    return css, 200, {'Content-Type': 'text/css', 'Cache-Control': 'public, max-age=360'}

@app.route('/fonts/<path:filename>')
async def serve_fonts(filename):
    """Serve font files from resources/fonts directory."""
    fonts_dir = RESOURCES_DIR / "fonts"
    return await send_from_directory(str(fonts_dir), filename)

# --- Routes ---

@app.route("/health")
async def health():
    """
    Health check endpoint for Docker/Kubernetes.
    Returns basic status info for container orchestration.
    """
    # Check Spotify authentication status
    client = get_spotify_client()
    spotify_status = "authenticated" if client else "not_configured"
    
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - APP_START_TIME),
        "spotify": spotify_status
    }, 200

@app.route("/")
async def index():
    """Render main UDP-only web UI."""
    try:
        return await render_template(
            "index.html",
            spotify_auth_url=None,
            spotify_needs_auth=False,
            configured_redirect_uri=None,
            suggested_redirect_uri=None,
        )
    except Exception as e:
        logger.error(f"Error rendering index: {e}", exc_info=True)
        return f"Error rendering template: {str(e)}", 500


@app.route("/lyrics")
async def lyrics() -> dict:
    """
    API endpoint that returns lyrics data as JSON.
    Called by the frontend JavaScript to fetch lyrics updates.

    If ``?player=<name>`` is supplied and multi-instance mode is active, the
    handler runs under ``lyrics_module.scoped_player_state(player_name)`` which
    swaps the module-level lyrics globals with that player's snapshot and sets
    the metadata player hint. This way the fetch pipeline keys off the correct
    song per player instead of whichever engine was registered first.
    """
    player_scope = _player_name_from_request()
    if player_scope:
        mgr = _get_player_manager_if_running()
        if mgr is not None:
            scoped_song = mgr.get_current_song(player_scope)
            if not scoped_song:
                scoped_colors = ["#24273a", "#363b54"]
                return {
                    "lyrics": [],
                    "msg": f"Waiting for player '{player_scope}'...",
                    "colors": scoped_colors,
                    "provider": None,
                    "has_lyrics": False,
                    "is_instrumental": False,
                    "is_instrumental_manual": False,
                    "word_synced_lyrics": None,
                    "has_word_sync": False,
                    "word_sync_provider": None,
                    "player": player_scope,
                }

    async with lyrics_module.scoped_player_state(player_scope):
        return await _build_lyrics_response(player_scope)


async def _build_lyrics_response(player_scope: Optional[str]) -> dict:
    lyrics_data = await get_timed_lyrics_previous_and_next()
    metadata = await get_current_song_meta_data()
    
    # Remove the early return for string type so we can wrap it properly
    # if isinstance(lyrics_data, str):
    #    return {"msg": lyrics_data}
    
    colors = ["#24273a", "#363b54"]
    if metadata and metadata.get("colors"):
        colors = metadata.get("colors")
    
    provider = get_current_provider()
    
    # Determine flags
    is_instrumental = False
    has_lyrics = True
    is_instrumental_manual = False
    
    # Check if song is manually marked as instrumental
    if metadata:
        artist = metadata.get("artist", "")
        title = metadata.get("title", "")
        if artist and title:
            is_instrumental_manual = _is_manually_instrumental(artist, title)
            if is_instrumental_manual:
                # Manually marked as instrumental - override detection
                is_instrumental = True
                has_lyrics = False
            # Also check cached metadata from providers (e.g., Musixmatch returns is_instrumental flag)
            elif _is_cached_instrumental(artist, title):
                is_instrumental = True
                has_lyrics = False
    
    if isinstance(lyrics_data, str):
        # Handle error messages or status strings
        msg = lyrics_data
        has_lyrics = False
        
        # Check for specific status messages (only if not manually marked)
        if not is_instrumental_manual and "instrumental" in msg.lower():
            is_instrumental = True
            
        return {
            "lyrics": [], 
            "msg": msg,
            "colors": colors, 
            "provider": provider,
            "has_lyrics": False,
            "is_instrumental": is_instrumental,
            "is_instrumental_manual": is_instrumental_manual,
            "word_synced_lyrics": None,
            "has_word_sync": False,
            "word_sync_provider": None
        }
    
    # Check if lyrics are actually empty or just [...]
    # (lyrics_data is a tuple of strings)
    if not lyrics_data or all(not line for line in lyrics_data):
         has_lyrics = False
    
    # FIX: Check instrumental using RAW cached lyrics, not the display tuple
    # The display tuple always has 6 elements, so len()==1 was never true before
    # This also checks the metadata is_instrumental flag saved by providers like Musixmatch
    if not is_instrumental_manual:
        current_lyrics = lyrics_module.current_song_lyrics
        if current_lyrics and len(current_lyrics) == 1:
            text = current_lyrics[0][1].lower().strip() if len(current_lyrics[0]) > 1 else ""
            if text in ["instrumental", "music only", "no lyrics", "non-lyrical", "♪", "♫", "♬", "(instrumental)", "[instrumental]"]:
                is_instrumental = True
                has_lyrics = False

    # Get word-synced lyrics data (for karaoke-style display)
    word_synced_lyrics = lyrics_module.current_song_word_synced_lyrics
    word_sync_provider = lyrics_module.current_word_sync_provider
    has_word_sync = word_synced_lyrics is not None and len(word_synced_lyrics) > 0
    
    # Check if ANY cached provider has word-sync (for toggle availability)
    # This allows the toggle to be enabled even if current provider doesn't have word-sync
    any_provider_has_word_sync = has_word_sync  # Initially same as current
    if not any_provider_has_word_sync and lyrics_module.current_song_data:
        artist = lyrics_module.current_song_data.get("artist", "")
        title = lyrics_module.current_song_data.get("title", "")
        if artist and title:
            any_provider_has_word_sync = lyrics_module._has_any_word_sync_cached(artist, title)

    # Build line-synced lyrics timing data for smooth frontend animation
    # Includes start timestamp for each line so the frontend can do smooth
    # pixel scrolling, font inflate/deflate, and line highlighting
    line_synced_lyrics = None
    if lyrics_module.current_song_lyrics and len(lyrics_module.current_song_lyrics) > 1:
        line_synced_lyrics = [
            {"start": line[0], "text": line[1]}
            for line in lyrics_module.current_song_lyrics
        ]

    # Extract instrumental markers from line-sync data (for gap detection in word-sync mode)
    # These are explicit ♪ markers from Spotify/Musixmatch that indicate instrumental breaks
    # We explicitly check Spotify/Musixmatch from cache (authoritative sources), even if not current provider
    # PERFORMANCE: Cache markers per song to avoid disk reads every 100ms poll
    instrumental_markers = []
    
    if metadata:
        artist = metadata.get("artist", "")
        title = metadata.get("title", "")
        cache_key = (artist, title) if artist and title else None
        
        # Check if we have cached markers for this song
        if cache_key and _instrumental_markers_cache['key'] == cache_key:
            # Use cached markers (no disk read needed)
            instrumental_markers = _instrumental_markers_cache['markers']
        elif cache_key:
            # Song changed - invalidate cache and extract markers from disk
            instrumental_symbols = {'♪', '♫', '♬', '🎵', '🎶'}
            
            try:
                # Get the db path and read cached providers
                db_path = lyrics_module._get_db_path(artist, title)
                if db_path and os.path.exists(db_path):
                    with open(db_path, 'r', encoding='utf-8') as f:
                        cached_data = json.load(f)
                    
                    saved_lyrics = cached_data.get("saved_lyrics", {})
                    
                    # Priority: Spotify first, then Musixmatch
                    for provider_name in ["spotify", "musixmatch"]:
                        if provider_name in saved_lyrics:
                            provider_lyrics = saved_lyrics[provider_name]
                            for line in provider_lyrics:
                                if len(line) >= 2:
                                    timestamp, text = line[0], line[1]
                                    if text.strip() in instrumental_symbols:
                                        instrumental_markers.append(timestamp)
                            
                            # If we found markers, stop (use highest priority source)
                            if instrumental_markers:
                                break
            except Exception as e:
                logger.debug(f"Could not load Spotify/Musixmatch markers from cache: {e}")
            
            # Fallback: If no markers found from Spotify/Musixmatch, check current provider
            if not instrumental_markers and lyrics_module.current_song_lyrics:
                for line in lyrics_module.current_song_lyrics:
                    if len(line) >= 2:
                        timestamp, text = line[0], line[1]
                        if text.strip() in instrumental_symbols:
                            instrumental_markers.append(timestamp)
            
            # Update cache for this song
            _instrumental_markers_cache['key'] = cache_key
            _instrumental_markers_cache['markers'] = instrumental_markers

    # Include track_id so the frontend can detect stale lyrics from the
    # recognition engine (which lags behind MA's instant track identity).
    lyrics_track_id = None
    if metadata:
        from system_utils.helpers import _normalize_track_id
        a = metadata.get("artist", "")
        t = metadata.get("title", "")
        if a and t:
            lyrics_track_id = _normalize_track_id(a, t)

    return {
        "lyrics": list(lyrics_data),
        "colors": colors,
        "provider": provider,
        "has_lyrics": has_lyrics,
        "is_instrumental": is_instrumental,
        "is_instrumental_manual": is_instrumental_manual,
        # Word-synced lyrics for karaoke-style display
        "word_synced_lyrics": word_synced_lyrics if has_word_sync else None,
        "has_word_sync": has_word_sync,
        "word_sync_provider": word_sync_provider if has_word_sync else None,
        # Flag for toggle availability: true if ANY cached provider has word-sync
        "any_provider_has_word_sync": any_provider_has_word_sync,
        # Instrumental markers for gap detection (timestamps where ♪ appears in line-sync)
        "instrumental_markers": instrumental_markers if instrumental_markers else None,
        # Line-synced lyrics timing data for smooth frontend animation
        "line_synced_lyrics": line_synced_lyrics,
        # Track ID the lyrics belong to (frontend discards if it doesn't match current track)
        "track_id": lyrics_track_id
    }

def _get_player_manager_if_running():
    """Return the PlayerManager if multi-instance mode is active, else None."""
    import sys
    if 'audio_recognition.player_manager' not in sys.modules:
        return None
    try:
        from audio_recognition.player_manager import get_player_manager
        mgr = get_player_manager()
        return mgr if mgr.is_running else None
    except Exception:
        return None


def _player_name_from_request() -> Optional[str]:
    """Extract a ?player=<name> query param, trimmed and validated."""
    name = request.args.get("player") if request else None
    if not name:
        return None
    name = name.strip()
    return name or None


def _build_player_track_payload(player_name: str) -> Optional[dict]:
    """
    Build a /current-track-compatible payload directly from a player's
    RecognitionEngine, bypassing the multi-source metadata orchestrator.
    Returns None if the player or its song is unknown.
    """
    mgr = _get_player_manager_if_running()
    if mgr is None:
        return None
    song = mgr.get_current_song(player_name)
    if not song:
        return None
    position = mgr.get_current_position(player_name) or 0.0
    duration_ms = song.get("duration_ms") or 0
    duration_sec = duration_ms / 1000.0 if duration_ms else 0
    artist = song.get("artist", "")
    title = song.get("title", "")
    metadata = {
        "source": "audio_recognition",
        "player": player_name,
        "artist": artist,
        "title": title,
        "album": song.get("album"),
        "album_art": song.get("album_art_url"),
        "album_art_url": song.get("album_art_url"),
        "artist_id": song.get("artist_id"),
        "artist_name": song.get("artist_name") or artist,
        "track_id": song.get("track_id"),
        "id": song.get("id"),
        # Frontend reads `position` (seconds) and `duration_ms` (ms);
        # keep `progress`/`duration` for any legacy callers.
        "position": position,
        "progress": int(position * 1000),
        "duration": duration_sec,
        "duration_ms": int(duration_ms),
        "is_playing": True,
        "isrc": song.get("isrc"),
        "spotify_url": song.get("spotify_url"),
        "colors": song.get("colors"),
        "recognition_provider": song.get("recognition_provider"),
    }
    return metadata


@app.route("/api/players")
async def api_players() -> dict:
    """
    List configured players, discovered-but-unassigned streams, and
    per-player engine status. Used by the settings UI to wire streams
    to players.
    """
    from audio_recognition.player_registry import get_registry
    registry = get_registry()
    configured = [
        {
            "name": p.name,
            "display_name": p.display_name or p.name,
            "source_ip": p.source_ip,
            "rtp_ssrc": f"0x{p.rtp_ssrc:08X}" if p.rtp_ssrc is not None else None,
            "music_assistant_player_id": p.music_assistant_player_id,
            "description": p.description,
            "auto": p.auto,
        }
        for p in registry.list_players()
    ]
    discovered = [s.to_dict() for s in registry.list_discovered()]
    mgr = _get_player_manager_if_running()
    engines = mgr.list_engine_status() if mgr else []
    streams = mgr.list_streams() if mgr else []
    return jsonify({
        "multi_instance_active": mgr is not None,
        "configured": configured,
        "discovered": discovered,
        "engines": engines,
        "streams": streams,
    })


@app.route("/api/players/<player_name>/track")
async def api_player_track(player_name: str):
    """Return the current track for a specific player (no fallback)."""
    payload = _build_player_track_payload(player_name)
    if payload is None:
        return jsonify({"error": f"no track for player '{player_name}'"}), 404
    return jsonify(payload)


@app.route("/api/players/<player_name>/rename", methods=["POST"])
async def api_player_rename(player_name: str):
    """
    Set a friendly display name for an auto-detected (or configured) player.
    Body: {"display_name": "Study"}  or  {"display_name": "", "music_assistant_player_id": "ma_id"}
    """
    try:
        body = await request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    display_name = (body.get("display_name") or "").strip()
    ma_player_id = body.get("music_assistant_player_id")
    from audio_recognition.player_registry import get_registry
    registry = get_registry()
    ok = registry.rename(player_name, display_name)
    if not ok:
        return jsonify({"error": f"unknown player '{player_name}'"}), 404
    if ma_player_id is not None:
        registry.set_music_assistant_player(player_name, ma_player_id or None)
    return jsonify({"ok": True, "display_name": display_name or player_name})


@app.route("/api/music-assistant/players", methods=["GET"])
async def api_ma_players():
    """
    Return the list of Music Assistant players so the UI can offer them as
    naming suggestions for auto-detected RTP sources. Safe no-op when MA
    isn't configured / reachable — returns an empty list.
    """
    try:
        from system_utils.sources.music_assistant import MusicAssistantSource, is_configured
    except Exception:
        return jsonify({"players": [], "configured": False})
    if not is_configured():
        return jsonify({"players": [], "configured": False})
    try:
        ma = MusicAssistantSource()
        devices = await ma.get_devices()
    except Exception as exc:
        logger.debug(f"MA players fetch failed: {exc}")
        devices = []
    return jsonify({"players": devices, "configured": True})


@app.route("/api/players/bind", methods=["POST"])
async def api_players_bind():
    """
    Manually bind a discovered stream to a configured player.
    Body: {"source_ip": "...", "ssrc": null | int | "0x...", "player": "name"}
    """
    try:
        body = await request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    source_ip = (body.get("source_ip") or "").strip()
    player = (body.get("player") or "").strip()
    ssrc_raw = body.get("ssrc")
    ssrc: Optional[int] = None
    if ssrc_raw not in (None, "", "null"):
        try:
            ssrc = int(str(ssrc_raw), 0) & 0xFFFFFFFF
        except (ValueError, TypeError):
            return jsonify({"error": "invalid ssrc"}), 400
    if not source_ip or not player:
        return jsonify({"error": "source_ip and player are required"}), 400
    from audio_recognition.player_registry import get_registry
    ok = get_registry().bind(source_ip, ssrc, player)
    if not ok:
        return jsonify({"error": f"unknown player '{player}'"}), 404
    return jsonify({"ok": True})


@app.route("/current-track")
async def current_track() -> dict:
    """
    Returns detailed track info (Art, Progress, Duration).
    Used for the UI Header/Footer.
    Includes artist_id for visual mode and artist image fetching.

    If ``?player=<name>`` is supplied and the PlayerManager knows that
    player, the response is sourced from that player's recognition engine
    instead of the global metadata orchestrator. This lets multiple
    displays on the same server each show a different speaker group.
    """
    player_scope = _player_name_from_request()
    if not player_scope:
        # No explicit player — if multi-instance mode is active, fall back to
        # the first player with a live track so the default homepage still
        # displays something useful.
        mgr = _get_player_manager_if_running()
        if mgr is not None:
            for engine in mgr.list_engines().values():
                if engine.last_result is not None:
                    player_scope = engine.player_name
                    break
            if not player_scope and mgr.list_engines():
                player_scope = next(iter(mgr.list_engines().keys()))

    if player_scope:
        scoped = _build_player_track_payload(player_scope)
        if scoped is None:
            return {"error": f"no track for player '{player_scope}'", "player": player_scope}
        # Apply the same latency-compensation fields the single-player path adds.
        latency_comp = LYRICS.get("display", {}).get("audio_recognition_latency_compensation", 0.0)
        scoped["latency_compensation"] = latency_comp
        scoped["word_sync_latency_compensation"] = LYRICS.get("display", {}).get("word_sync_latency_compensation", 0.0)
        scoped["provider_word_sync_offset"] = 0.0
        scoped["word_sync_provider"] = None
        scoped["word_sync_default_enabled"] = settings.get("features.word_sync_default_enabled", True)
        scoped_artist = scoped.get("artist")
        scoped_title = scoped.get("title")
        scoped["song_word_sync_offset"] = (
            lyrics_module.get_song_word_sync_offset(scoped_artist, scoped_title)
            if scoped_artist and scoped_title else 0.0
        )
        scoped["is_instrumental"] = False
        scoped["is_instrumental_manual"] = False
        # Drive the on-screen timeline from the linked Music Assistant
        # player rather than the recognition engine, which never has a real
        # duration and only updates on each match. Falls through silently
        # when MA isn't reachable for this player.
        #
        # IMPORTANT: always call get_metadata() even when ma_player_id is None
        # (auto-detect mode).  This populates _current_queue_id and
        # _current_player_id in the MA module so that subsequent transport
        # control commands can resolve the queue without an explicit link.
        try:
            ma_player_id = await _resolve_ma_player_id_for_request()
            from system_utils.sources.music_assistant import MusicAssistantSource, is_configured
            if is_configured():
                ma_meta = await MusicAssistantSource(target_player_id=ma_player_id).get_metadata()
                if ma_meta:
                    # Timeline fields: always override from MA (more accurate than recognition engine)
                    # EXCEPT for radio streams (where MA position is stream uptime, not track position)
                    is_radio = ma_meta.get("media_type") == "radio" or not ma_meta.get("duration_ms")
                    
                    # Track identity fields: override from MA so the frontend sees
                    # track changes instantly instead of waiting 10-15s for the
                    # recognition engine to re-identify audio it already knew was next.
                    # Only apply if MA has a valid track (artist + title both present).
                    ma_artist = ma_meta.get("artist")
                    ma_title = ma_meta.get("title")
                    
                    is_lagging = ma_artist and ma_title and (ma_artist != scoped.get("artist") or ma_title != scoped.get("title"))
                    
                    for key in ("position", "duration_ms", "is_playing"):
                        value = ma_meta.get(key)
                        if value is not None:
                            if key == "position" and is_radio:
                                if is_lagging:
                                    scoped[key] = 0.0
                                continue
                            scoped[key] = value
                    
                    if ma_artist and ma_title:
                        scoped["artist"] = ma_artist
                        scoped["artist_name"] = ma_meta.get("artist_name") or ma_artist
                        scoped["title"] = ma_title
                        scoped["album"] = ma_meta.get("album") or scoped.get("album")
                        scoped["track_id"] = ma_meta.get("track_id") or scoped.get("track_id")
                        scoped["ma_item_id"] = ma_meta.get("ma_item_id")
                        # Clear stale artist_id from recognition engine. MA doesn't
                        # provide Spotify artist_id, and keeping the old one causes
                        # the frontend to detect a false artist change when the
                        # recognition engine eventually catches up with its own
                        # artist_id, leading to slideshow images mixing.
                        scoped["artist_id"] = None
                        # Use MA album art if available (recognition engine art may be stale)
                        ma_art = ma_meta.get("album_art_url")
                        if ma_art:
                            scoped["album_art_url"] = ma_art
                            scoped["album_art"] = ma_art
                else:
                    # MA configured but state unknown — send null so the frontend
                    # preserves its current animation/icon state rather than
                    # defaulting to the hardcoded is_playing:True from the UDP engine.
                    scoped["is_playing"] = None
        except Exception as exc:
            logger.debug(f"MA timeline override failed: {exc}")
        return scoped

    try:
        metadata = await get_current_song_meta_data()
        if metadata:
            # Check for manual instrumental flag first (takes precedence)
            artist = metadata.get("artist", "")
            title = metadata.get("title", "")
            is_instrumental_manual = False
            is_instrumental = False
            
            if artist and title:
                is_instrumental_manual = _is_manually_instrumental(artist, title)
                if is_instrumental_manual:
                    # Manually marked as instrumental - override detection
                    is_instrumental = True
                # Check cached metadata from providers (e.g., Musixmatch returns is_instrumental flag)
                elif _is_cached_instrumental(artist, title):
                    is_instrumental = True
                else:
                    # Fall back to automatic detection via lyrics text
                    current_lyrics = lyrics_module.current_song_lyrics
                    if current_lyrics and len(current_lyrics) == 1:
                        text = current_lyrics[0][1].lower().strip()
                        # Updated list to match lyrics.py
                        if text in ["instrumental", "music only", "no lyrics", "non-lyrical", "♪", "♫", "♬", "(instrumental)", "[instrumental]"]:
                            is_instrumental = True
            
            metadata["is_instrumental"] = is_instrumental
            metadata["is_instrumental_manual"] = is_instrumental_manual
            
            # Add latency compensation for word-sync (based on source)
            # Same logic as _find_current_lyric_index in lyrics.py
            source = metadata.get("source", "")
            if source == "spotify":
                # Spotify-only mode (e.g., HAOS without Windows)
                latency_comp = LYRICS.get("display", {}).get("spotify_latency_compensation", -0.5)
            elif source == "audio_recognition":
                # Audio recognition mode
                latency_comp = LYRICS.get("display", {}).get("audio_recognition_latency_compensation", 0.0)
            elif source == "music_assistant":
                # Music Assistant mode (network streaming via MA server)
                latency_comp = LYRICS.get("display", {}).get("music_assistant_latency_compensation", 0.0)
            else:
                latency_comp = LYRICS.get("display", {}).get("latency_compensation", 0.0)
            metadata["latency_compensation"] = latency_comp
            
            # Add separate word-sync latency compensation for fine-tuning karaoke timing
            word_sync_latency_comp = LYRICS.get("display", {}).get("word_sync_latency_compensation", 0.0)
            metadata["word_sync_latency_compensation"] = word_sync_latency_comp
            
            # Add provider-specific word-sync offset (Musixmatch/NetEase may have different timing)
            # Use settings.get() instead of LYRICS dict for hot-reload support
            word_sync_provider = lyrics_module.current_word_sync_provider
            provider_offset = 0.0
            if word_sync_provider:
                offset_key = f"lyrics.display.{word_sync_provider}_word_sync_offset"
                provider_offset = settings.get(offset_key, 0.0)
            metadata["provider_word_sync_offset"] = provider_offset
            metadata["word_sync_provider"] = word_sync_provider
            
            # Add word-sync default enabled setting (frontend can still toggle)
            word_sync_default = settings.get("features.word_sync_default_enabled", True)
            metadata["word_sync_default_enabled"] = word_sync_default
            
            # Add per-song word-sync offset (user adjustment)
            song_offset = lyrics_module.get_song_word_sync_offset(artist, title)
            metadata["song_word_sync_offset"] = song_offset
            
            return metadata
        return {"error": "No track playing"}
    except Exception as e:
        logger.error(f"Track Info Error: {e}")
        return {"error": str(e)}


@app.route('/api/word-sync-offset', methods=['POST'])
async def save_word_sync_offset():
    """
    Save per-song word-sync offset adjustment.
    Frontend calls this when user adjusts latency via UI buttons.
    """
    try:
        data = await request.json
        artist = data.get('artist')
        title = data.get('title')
        
        # Defensive validation: handle NaN, Infinity, strings, null
        try:
            offset = float(data.get('offset', 0.0))
            if not (-10.0 <= offset <= 10.0) or offset != offset:  # Check NaN
                offset = 0.0
        except (TypeError, ValueError):
            offset = 0.0
        
        if not artist or not title:
            return {"success": False, "error": "Missing artist or title"}
        
        success = await lyrics_module.save_song_word_sync_offset(artist, title, offset)
        
        if success:
            return {"success": True, "offset": offset}
        else:
            return {"success": False, "error": "Failed to save offset"}
    except Exception as e:
        logger.error(f"Word-sync offset error: {e}")
        return {"success": False, "error": str(e)}


@app.route('/api/settings/reload', methods=['POST'])
async def reload_settings():
    """
    Reload settings from disk without restarting the server.
    Useful for applying backend config changes on the fly.
    """
    try:
        settings.load_settings()
        logger.info("Settings reloaded from disk")
        return {"success": True, "message": "Settings reloaded"}
    except Exception as e:
        logger.error(f"Failed to reload settings: {e}")
        return {"success": False, "error": str(e)}


# --- Audio Analysis API (for waveform and spectrum visualizer) ---

@app.route('/api/playback/audio-analysis')
async def get_audio_analysis():
    """Return 404 because legacy Spicetify audio-analysis caches are not bundled."""
    return jsonify({"error": "No audio analysis available"}), 404


# --- PWA Routes ---

@app.route('/manifest.json')
async def manifest():
    """
    Serve the PWA manifest.json file with correct MIME type and icon paths.
    This enables Progressive Web App installation on Android devices.
    We generate it dynamically to ensure icon paths use the correct static URL.
    """
    import json
    
    # Generate manifest with correct icon URLs using url_for
    manifest_data = {
        "name": "SyncLyrics",
        "short_name": "SyncLyrics",
        "description": "Real-time synchronized lyrics display",
        "start_url": "/",
        "scope": "/",
        "display": "fullscreen",
        "orientation": "any",
        "theme_color": "#1db954",
        "background_color": "#000000",
        "categories": ["music", "entertainment"],
        "icons": [
            {
                "src": url_for('static', filename='images/icon-192.png'),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": url_for('static', filename='images/icon-512.png'),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": url_for('static', filename='images/icon-maskable.png'),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable"
            }
        ]
    }
    
    # Return as JSON with correct MIME type
    response = jsonify(manifest_data)
    response.headers['Content-Type'] = 'application/manifest+json'
    return response

# --- Settings API (Unchanged) ---

@app.route("/api/settings", methods=['GET'])
async def api_get_settings():
    return jsonify(settings.get_all())

@app.route("/api/settings/<key>", methods=['POST'])
async def api_update_setting(key: str):
    try:
        data = await request.get_json()
        if 'value' not in data: return jsonify({"error": "No value"}), 400
        needs_restart = settings.set(key, data['value'])
        settings.save_to_config()
        return jsonify({"success": True, "requires_restart": needs_restart})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/settings", methods=['POST'])
async def api_update_settings():
    try:
        data = await request.get_json()
        needs_restart = False
        for key, value in data.items():
            needs_restart |= settings.set(key, value)
        settings.save_to_config()
        return jsonify({"success": True, "requires_restart": needs_restart})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# --- Provider Management API ---

@app.route("/api/providers/current", methods=['GET'])
async def get_current_provider_info():
    """Get info about the provider currently serving lyrics"""
    from lyrics import get_current_provider, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"error": "No song playing"}), 404
    
    provider_name = get_current_provider()
    if not provider_name:
        return jsonify({"error": "No provider active"}), 404
    
    # Find provider object for additional info
    from lyrics import providers
    provider_info = None
    for p in providers:
        if p.name == provider_name:
            provider_info = {
                "name": p.name,
                "priority": p.priority,
                "enabled": p.enabled
            }
            break
    
    return jsonify(provider_info or {"name": provider_name})

@app.route("/api/providers/available", methods=['GET'])
async def get_available_providers():
    """Get list of providers that could provide lyrics for current song"""
    from lyrics import get_available_providers_for_song, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"error": "No song playing"}), 404
    
    artist = song_data.get("artist", "")
    title = song_data.get("title", "")
    
    if not artist and not title:
        return jsonify({"error": "Invalid song data"}), 400
    
    providers_list = get_available_providers_for_song(artist, title)
    return jsonify({"providers": providers_list})

@app.route("/api/providers/preference", methods=['POST'])
async def set_provider_preference():
    """Set preferred provider for current song"""
    from lyrics import set_provider_preference as set_pref, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"error": "No song playing"}), 404
    
    data = await request.get_json()
    provider_name = data.get('provider')
    
    if not provider_name:
        return jsonify({"error": "No provider specified"}), 400
    
    artist = song_data.get("artist", "")
    title = song_data.get("title", "")
    
    result = await set_pref(artist, title, provider_name)
    
    if result['status'] == 'success':
        return jsonify(result), 200
    else:
        return jsonify(result), 400

@app.route("/api/providers/word-sync-preference", methods=['POST'])
async def set_word_sync_preference():
    """Set preferred word-sync provider for current song"""
    from lyrics import set_word_sync_provider_preference, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"error": "No song playing"}), 404
    
    data = await request.get_json()
    provider_name = data.get('provider')
    
    if not provider_name:
        return jsonify({"error": "No provider specified"}), 400
    
    artist = song_data.get("artist", "")
    title = song_data.get("title", "")
    
    result = await set_word_sync_provider_preference(artist, title, provider_name)
    
    if result['status'] == 'success':
        return jsonify(result), 200
    else:
        return jsonify(result), 400

@app.route("/api/providers/word-sync-preference", methods=['DELETE'])
async def clear_word_sync_preference():
    """Clear word-sync provider preference for current song"""
    from lyrics import clear_word_sync_provider_preference, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"error": "No song playing"}), 404
    
    artist = song_data.get("artist", "")
    title = song_data.get("title", "")
    
    success = await clear_word_sync_provider_preference(artist, title)
    
    if success:
        return jsonify({"status": "success", "message": "Word-sync preference cleared"}), 200
    else:
        return jsonify({"error": "Failed to clear preference"}), 400

@app.route("/api/instrumental/mark", methods=['POST'])
async def mark_instrumental():
    """
    Marks or unmarks the current song as instrumental manually.
    Body: {"is_instrumental": true/false}
    """
    try:
        data = await request.get_json()
        is_instrumental = data.get("is_instrumental", False)
        
        metadata = await get_current_song_meta_data()
        if not metadata:
            return jsonify({"error": "No track playing"}), 400
        
        artist = metadata.get("artist", "")
        title = metadata.get("title", "")
        
        if not artist or not title:
            return jsonify({"error": "Missing artist or title"}), 400
        
        success = await set_manual_instrumental(artist, title, is_instrumental)
        
        if success:
            # Force refresh lyrics to apply the change immediately
            # Clear current lyrics so it re-fetches with the new flag
            lyrics_module.current_song_lyrics = None
            lyrics_module.current_song_data = None
            
            return jsonify({
                "success": True,
                "is_instrumental": is_instrumental,
                "message": f"Song marked as {'instrumental' if is_instrumental else 'NOT instrumental'}"
            })
        else:
            return jsonify({"error": "Failed to update instrumental flag"}), 500
            
    except Exception as e:
        logger.error(f"Error marking instrumental: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/providers/preference", methods=['DELETE'])
async def clear_provider_preference_endpoint():
    """Clear provider preference for current song"""
    from lyrics import clear_provider_preference as clear_pref, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"error": "No song playing"}), 404
    
    artist = song_data.get("artist", "")
    title = song_data.get("title", "")
    
    success = await clear_pref(artist, title)
    
    if success:
        return jsonify({"status": "success", "message": "Preference cleared"}), 200
    else:
        return jsonify({"status": "error", "message": "Failed to clear preference"}), 500

@app.route("/api/lyrics/delete", methods=['DELETE'])
async def delete_cached_lyrics_endpoint():
    """Delete all cached lyrics for current song (use when lyrics are wrong)"""
    from lyrics import delete_cached_lyrics, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"error": "No song playing"}), 404
    
    artist = song_data.get("artist", "")
    title = song_data.get("title", "")
    
    if not artist or not title:
        return jsonify({"error": "Invalid song data"}), 400
    
    result = await delete_cached_lyrics(artist, title)
    
    if result['status'] == 'success':
        return jsonify(result), 200
    else:
        return jsonify(result), 500


@app.route("/api/backfill/lyrics", methods=['POST'])
async def backfill_lyrics_endpoint():
    """Manually trigger lyrics refetch from ALL enabled providers"""
    from lyrics import refetch_lyrics, current_song_data
    
    # Use lyrics cache if available, otherwise get fresh metadata (handles paused state)
    song_data = current_song_data
    if not song_data:
        song_data = await get_current_song_meta_data()
    if not song_data:
        return jsonify({"status": "error", "message": "No song playing"}), 404
    
    artist = song_data.get("artist", "")
    title = song_data.get("title", "")
    album = song_data.get("album")
    duration_ms = song_data.get("duration_ms")
    duration = duration_ms // 1000 if duration_ms else None
    
    if not artist or not title:
        return jsonify({"status": "error", "message": "Invalid song data"}), 400
    
    result = await refetch_lyrics(artist, title, album, duration)
    return jsonify(result), 200 if result['status'] == 'success' else 500


@app.route("/api/backfill/art", methods=['POST'])
async def backfill_art_endpoint():
    """Manually trigger album art and artist images refetch"""
    from system_utils import get_current_song_meta_data, ensure_album_art_db, ensure_artist_image_db
    
    metadata = await get_current_song_meta_data()
    if not metadata:
        return jsonify({"status": "error", "message": "No song playing"}), 404
    
    artist = metadata.get("artist", "")
    album = metadata.get("album")
    title = metadata.get("title")
    spotify_url = metadata.get("album_art_url")
    artist_id = metadata.get("artist_id")
    
    if not artist:
        return jsonify({"status": "error", "message": "Invalid song data"}), 400
    
    logger.info(f"Manual Refetch Art triggered for: {artist} - {title}")
    
    # Trigger both album art and artist images refetch with force=True
    from system_utils.helpers import create_tracked_task

    async def run_refetch():
        # Refetch album art
        await ensure_album_art_db(artist, album, title, spotify_url, retry_count=0, force=True)
        # Refetch artist images
        await ensure_artist_image_db(artist, artist_id, force=True)
    
    create_tracked_task(run_refetch())
    
    return jsonify({
        "status": "success",
        "message": "Refetching album art and artist images..."
    }), 200


# --- Album Art Database API ---

@app.route("/api/album-art/options", methods=['GET'])
async def get_album_art_options():
    """Get available album art options for current track from database, including artist images"""
    from system_utils import get_current_song_meta_data, load_album_art_from_db, get_album_db_folder
    from config import ALBUM_ART_DB_DIR
    from pathlib import Path
    import json
    from urllib.parse import quote
    
    metadata = await get_current_song_meta_data()
    if not metadata:
        return jsonify({"error": "No song playing"}), 404
    
    artist = metadata.get("artist", "")
    album = metadata.get("album")
    title = metadata.get("title")  # Get title for fallback when album is missing (singles)
    
    if not artist:
        return jsonify({"error": "Invalid song data"}), 400
    
    # CRITICAL FIX: Use title as fallback when album is missing (for singles)
    # This ensures we look in the correct folder: "Artist - Title" instead of just "Artist"
    # This matches the logic used in system_utils.py ensure_album_art_db() and load_album_art_from_db()
    album_or_title = album if album else title
    
    # Load album art from database
    # CRITICAL FIX: Pass album and title explicitly to match function signature
    db_result = load_album_art_from_db(artist, album, title)
    options = []
    preferred_provider = None
    
    if db_result:
        db_metadata = db_result["metadata"]
        providers = db_metadata.get("providers", {})
        preferred_provider = db_metadata.get("preferred_provider")
        
        # Build folder path for album art
        # CRITICAL FIX: Use title as fallback when album is missing (for singles)
        # This ensures we build the correct folder path: "Artist - Title" instead of just "Artist"
        folder_path = get_album_db_folder(artist, album_or_title or db_metadata.get('album'))
        folder_name = folder_path.name
        
        # Add album art options
        for provider_name, provider_data in providers.items():
            encoded_folder = quote(folder_name, safe='')
            encoded_filename = quote(provider_data.get('filename', f'{provider_name}.jpg'), safe='')
            image_url = f"/api/album-art/image/{encoded_folder}/{encoded_filename}"
            
            options.append({
                "provider": provider_name,
                "url": provider_data.get("url"),
                "image_url": image_url,
                "resolution": provider_data.get("resolution", "unknown"),
                "width": provider_data.get("width", 0),
                "height": provider_data.get("height", 0),
                "is_preferred": provider_name == preferred_provider,
                "type": "album_art"  # Distinguish from artist images
            })
    
    # Also load artist images from artist-only folder
    artist_folder = get_album_db_folder(artist, None)  # Artist-only folder
    artist_metadata_path = artist_folder / "metadata.json"
    
    if artist_metadata_path.exists():
        try:
            with open(artist_metadata_path, 'r', encoding='utf-8') as f:
                artist_metadata = json.load(f)
            
            # Check if this is artist images metadata (type: "artist_images")
            if artist_metadata.get("type") == "artist_images":
                artist_images = artist_metadata.get("images", [])
                folder_name = artist_folder.name
                
                # CRITICAL FIX: Read artist image preference from ALBUM folder, not artist folder
                # Preferences are now stored per-album as preferred_artist_image_filename
                # The db_result contains album metadata which has this field
                album_preferred_artist_filename = None
                if db_result and db_result.get("metadata"):
                    album_preferred_artist_filename = db_result["metadata"].get("preferred_artist_image_filename")
                
                # Convert artist images to options format
                # CRITICAL FIX: Count images per source to create unique provider names when needed
                source_counts = {}
                for img in artist_images:
                    if img.get("downloaded") and img.get("filename"):
                        source = img.get("source", "Unknown")
                        source_counts[source] = source_counts.get(source, 0) + 1
                
                for img in artist_images:
                    if not img.get("downloaded") or not img.get("filename"):
                        continue
                    
                    source = img.get("source", "Unknown")
                    
                    # CRITICAL FIX: Filter out iTunes and LastFM from artist images
                    # These providers don't work for artist images (they only work for album art)
                    # iTunes Search API is designed for app icons and album art, not artist photos
                    # LastFM artist images are often low-quality placeholders
                    if source in ["iTunes", "LastFM", "Last.fm"]:
                        continue  # Skip these providers for artist images
                    
                    filename = img.get("filename")
                    img_url = img.get("url", "")
                    
                    # CRITICAL FIX: Create unique provider name when multiple images from same source
                    # If there are multiple images from the same source, include filename to make it unique
                    # This allows users to select the specific image they want, not just the first one
                    # UI Display: Clean names without "(Artist)" suffix - it's obvious from context
                    if source_counts.get(source, 0) > 1:
                        # Multiple images from this source - include filename for uniqueness
                        # Format: "FanArt.tv (fanart_tv_0.jpg)" - clean display name
                        provider_name = f"{source}"
                    else:
                        # Single image from this source - use simple format
                        provider_name = source
                    
                    # Build image URL
                    encoded_folder = quote(folder_name, safe='')
                    encoded_filename = quote(filename, safe='')
                    image_url = f"/api/album-art/image/{encoded_folder}/{encoded_filename}"
                    
                    # Try to get resolution from image file if available
                    image_path = artist_folder / filename
                    width = img.get("width", 0)
                    height = img.get("height", 0)
                    resolution = f"{width}x{height}" if width and height else "unknown"
                    
                    # CRITICAL FIX: Check preferred by FILENAME from album folder preference
                    # This uses the new per-album system (preferred_artist_image_filename in album metadata)
                    # Match by filename which is the most reliable identifier
                    is_preferred = (album_preferred_artist_filename == filename) if album_preferred_artist_filename else False
                    
                    options.append({
                        "provider": provider_name,
                        "url": img_url,  # Include URL for unique identification
                        "filename": filename,  # Include filename for unique identification
                        "image_url": image_url,
                        "resolution": resolution,
                        "width": width,
                        "height": height,
                        "is_preferred": is_preferred,
                        "type": "artist_image"  # Distinguish from album art
                    })
                
                # CRITICAL FIX: Update preferred_provider to reflect artist image preference if set
                # Use the album folder preference (filename-based) to find the source name for display
                if album_preferred_artist_filename:
                    # Find the source name for this filename to set as preferred_provider for API response
                    for img in artist_images:
                        if img.get("filename") == album_preferred_artist_filename:
                            preferred_provider = img.get("source", album_preferred_artist_filename)
                            break
        except Exception as e:
            logger.debug(f"Failed to load artist images metadata: {e}")
    
    # If no options found, return error
    if not options:
        return jsonify({"error": "No album art or artist image options found"}), 404
    
    return jsonify({
        "artist": artist,
        "album": album or (db_result["metadata"].get("album", "") if db_result else ""),
        "is_single": db_result["metadata"].get("is_single", False) if db_result else False,
        "preferred_provider": preferred_provider,
        "options": options
    })

@app.route("/api/album-art/preference", methods=['POST'])
async def set_album_art_preference():
    """Set preferred album art or artist image provider for current track"""
    from system_utils import get_current_song_meta_data, get_album_db_folder, load_album_art_from_db, save_album_db_metadata, _art_update_lock
    # Note: cleanup_old_art is imported at top of file (line 11), no need to re-import here
    from config import ALBUM_ART_DB_DIR, CACHE_DIR
    import shutil
    import os
    import json
    from datetime import datetime
    from pathlib import Path
    
    metadata = await get_current_song_meta_data()
    if not metadata:
        return jsonify({"error": "No song playing"}), 404
    
    data = await request.get_json()
    provider_name = data.get('provider')
    explicit_type = data.get('type')  # ADDED: Get explicit type from frontend (most reliable)
    
    if not provider_name:
        return jsonify({"error": "No provider specified"}), 400
    
    artist = metadata.get("artist", "")
    album = metadata.get("album")
    title = metadata.get("title")  # Get title for fallback when album is missing (singles)
    
    if not artist:
        return jsonify({"error": "Invalid song data"}), 400
    
    # CRITICAL FIX: Use title as fallback when album is missing (for singles)
    # This ensures we use the correct folder: "Artist - Title" instead of just "Artist"
    # This matches the logic used in system_utils.py ensure_album_art_db() and load_album_art_from_db()
    album_or_title = album if album else title
    
    # CRITICAL FIX: Validate that we have album_or_title for album art operations
    # This prevents corrupting artist images metadata if both album and title are missing
    # Artist images don't need album/title (they use artist-only folder), but album art does
    if not album_or_title:
        # Check if this is an artist image request - if so, we can proceed without album/title
        # Otherwise, return error for album art requests without album/title
        # OPTIMIZATION: Reuse explicit_type from line 617 instead of retrieving it again
        if not explicit_type or explicit_type != "artist_image":
            logger.error(f"Missing both album and title for artist '{artist}' - cannot set album art preference")
            return jsonify({"error": "Invalid song data: Missing album and title information"}), 400
    
    # CRITICAL FIX: Use explicit type from frontend if provided (most reliable)
    # This prevents ambiguity when provider names overlap between album art and artist images
    # (e.g., "iTunes", "Spotify" can exist in both, causing false positives)
    is_artist_image = False
    
    if explicit_type:
        # Frontend explicitly told us the type - trust it (most reliable method)
        is_artist_image = (explicit_type == "artist_image")
    else:
        # Fallback to detection logic (for backward compatibility with old frontend)
        # Since we removed "(Artist)" suffix from UI, we need to check by looking up in artist images
        try:
            # Check if provider_name matches any artist image in the database
            artist_folder = get_album_db_folder(artist, None)
            artist_metadata_path = artist_folder / "metadata.json"
            if artist_metadata_path.exists():
                with open(artist_metadata_path, 'r', encoding='utf-8') as f:
                    artist_metadata_check = json.load(f)
                if artist_metadata_check.get("type") == "artist_images":
                    artist_images_check = artist_metadata_check.get("images", [])
                    for img in artist_images_check:
                        source_check = img.get("source", "Unknown")
                        filename_check = img.get("filename", "")
                        # Check if provider_name matches any artist image format (with or without "(Artist)" suffix)
                        if (provider_name == source_check or 
                            provider_name == f"{source_check} ({filename_check})" or
                            provider_name == f"{source_check} (Artist)" or
                            provider_name == f"{source_check} ({filename_check}) (Artist)"):
                            is_artist_image = True
                            break
        except Exception:
            # Fallback: check by suffix (backward compatibility)
            is_artist_image = provider_name.endswith(" (Artist)")
    
    if is_artist_image:
        # Handle artist image preference
        # NEW 6.1: Save preference to ALBUM folder (per-album behavior)
        # Images still live in artist folder, but preference is per-album
        album_folder = get_album_db_folder(artist, album_or_title)  # Album folder for preference
        album_metadata_path = album_folder / "metadata.json"
        artist_folder = get_album_db_folder(artist, None)  # Artist folder for images
        artist_metadata_path = artist_folder / "metadata.json"
        
        if not artist_metadata_path.exists():
            return jsonify({"error": "No artist images database entry found"}), 404
        
        # CRITICAL FIX: Wrap entire Read-Modify-Write sequence in lock to prevent race conditions
        # This ensures that if a background task updates metadata simultaneously, we don't lose data
        # The lock makes the entire operation atomic: read -> modify -> save happens as one unit
        async with _art_update_lock:
            try:
                with open(artist_metadata_path, 'r', encoding='utf-8') as f:
                    artist_metadata = json.load(f)
            except (IOError, OSError, json.JSONDecodeError) as e:
                logger.error(f"Failed to load artist metadata: {e}")
                return jsonify({"error": "Failed to load artist images metadata"}), 500
            except Exception as e:
                logger.error(f"Unexpected error loading artist metadata: {e}", exc_info=True)
                return jsonify({"error": "Failed to load artist images metadata"}), 500
            
            # CRITICAL FIX: Match by provider name, URL, or filename to uniquely identify the selected image
            # This fixes the issue where multiple images from the same source (e.g., FanArt.tv) 
            # couldn't be distinguished, causing only the first one to be selected
            artist_images = artist_metadata.get("images", [])
            
            # Try to extract filename from provider name if it's in the format "Source (filename) (Artist)"
            # Otherwise, extract source name for backward compatibility
            matching_image = None
            
            # CRITICAL FIX: Match by filename first (most robust), then parse provider name
            # Priority: filename > URL > provider name parsing
            
            # 1. Match by filename if provided (MOST RELIABLE - from frontend)
            data_filename = data.get('filename')
            if data_filename:
                for img in artist_images:
                    if img.get("filename") == data_filename and img.get("downloaded"):
                        matching_image = img
                        break
            
            # 2. Match by URL if provided (also reliable)
            if not matching_image:
                data_url = data.get('url')
                if data_url:
                    for img in artist_images:
                        if img.get("url") == data_url and img.get("downloaded"):
                            matching_image = img
                            break
            
            # 3. Parse provider name (handles both old and new formats)
            if not matching_image:
                # Remove "(Artist)" suffix if present (backward compatibility)
                provider_name_clean = provider_name.replace(" (Artist)", "")
                
                # Check if provider name contains filename: "Source (filename)"
                if " (" in provider_name_clean:
                    parts = provider_name_clean.split(" (", 1)
                    if len(parts) == 2:
                        # Has filename: "Source (filename)"
                        source_name = parts[0]
                        filename_from_provider = parts[1].rstrip(")")
                        
                        # Match by source AND filename (case-insensitive source comparison)
                        source_name_lower = source_name.lower()  # Normalize to lowercase
                        for img in artist_images:
                            source = img.get("source", "")
                            if (source.lower() == source_name_lower and 
                                img.get("filename") == filename_from_provider and 
                                img.get("downloaded")):
                                matching_image = img
                                break
                    else:
                        # Fallback: just source name (case-insensitive)
                        source_name = parts[0]
                        source_name_lower = source_name.lower()
                        for img in artist_images:
                            source = img.get("source", "")
                            if source.lower() == source_name_lower and img.get("downloaded"):
                                matching_image = img
                                break
                else:
                    # No filename in provider name - match by source only (gets first match)
                    # CRITICAL FIX: Case-insensitive comparison to handle "Deezer" vs "deezer" mismatches
                    source_name = provider_name_clean
                    source_name_lower = source_name.lower()  # Normalize to lowercase for comparison
                    for img in artist_images:
                        source = img.get("source", "")
                        # Case-insensitive comparison to handle API inconsistencies
                        if source.lower() == source_name_lower and img.get("downloaded"):
                            matching_image = img
                            break
            
            if not matching_image:
                return jsonify({"error": f"Artist image '{provider_name}' not found in database"}), 404
            
            # Get the selected filename for saving
            selected_filename = matching_image.get("filename")
            
            # NEW 6.1: Save preference to ALBUM folder (per-album behavior)
            # Load or create album metadata
            try:
                if album_metadata_path.exists():
                    with open(album_metadata_path, 'r', encoding='utf-8') as f:
                        album_pref_metadata = json.load(f)
                else:
                    # Create new metadata for this album folder
                    album_pref_metadata = {
                        "type": "album_art",  # Keep compatible type
                        "artist": artist,
                        "album": album_or_title
                    }
            except Exception as e:
                logger.error(f"Failed to load album metadata for preference: {e}")
                # Create fresh metadata
                album_pref_metadata = {
                    "type": "album_art",
                    "artist": artist,
                    "album": album_or_title
                }
            
            # Save the per-album artist image preference
            album_pref_metadata["preferred_artist_image_filename"] = selected_filename
            album_pref_metadata["last_accessed"] = datetime.utcnow().isoformat() + "Z"
            
            # Ensure album folder exists and save
            album_folder.mkdir(parents=True, exist_ok=True)
            if not save_album_db_metadata(album_folder, album_pref_metadata):
                return jsonify({"error": "Failed to save artist image preference"}), 500
            
            # Log successful preference save for observability
            logger.info(f"Set artist image preference to '{provider_name}' for {artist} - {album_or_title}")
            
            # CRITICAL FIX: Clear artist image cache to ensure new preference is immediately reflected
            # Without this, the cache (15-second TTL) would continue serving the old image until it expires
            # Clear cache for the (artist, album) pair
            clear_artist_image_cache(artist)
            
            # Store filename for use outside lock
            filename = selected_filename
        
        # Copy selected image to cache for immediate use (outside lock to avoid blocking)
        db_image_path = artist_folder / filename
    else:
        # Handle album art preference (original logic)
        # CRITICAL FIX: Wrap entire Read-Modify-Write sequence in lock to prevent race conditions
        # This ensures that if a background task updates metadata simultaneously, we don't lose data
        # The lock makes the entire operation atomic: read -> modify -> save happens as one unit
        # CRITICAL FIX: Load metadata INSIDE the lock to ensure we get fresh data
        # (Loading before the lock could result in stale data if a background task updates between load and lock)
        async with _art_update_lock:
            # CRITICAL FIX: Use title as fallback when album is missing (for singles)
            # This ensures we look in the correct folder: "Artist - Title" instead of just "Artist"
            # CRITICAL FIX: Pass album and title explicitly to match function signature
            db_result = load_album_art_from_db(artist, album, title)
            if not db_result:
                return jsonify({"error": "No album art database entry found"}), 404
            
            db_metadata = db_result["metadata"]
            providers = db_metadata.get("providers", {})
            
            if provider_name not in providers:
                return jsonify({"error": f"Provider '{provider_name}' not found in database"}), 404
            
            # Update preferred provider
            db_metadata["preferred_provider"] = provider_name
            db_metadata["last_accessed"] = datetime.utcnow().isoformat() + "Z"
            
            # CRITICAL FIX: Clear artist image preference from album folder when album art is selected
            # The preference is stored as preferred_artist_image_filename in the album folder (per-album behavior)
            # This ensures album art takes priority over any previously selected artist image
            db_metadata["preferred_artist_image_filename"] = None
            
            # CRITICAL FIX: Clear artist image preference when album art is selected (mutual exclusion)
            # This ensures that selecting album art overrides any previously selected artist image
            # The user's last selection (album art) should take priority
            artist_folder_clear = get_album_db_folder(artist, None)  # Artist-only folder
            artist_metadata_path_clear = artist_folder_clear / "metadata.json"
            if artist_metadata_path_clear.exists():
                try:
                    with open(artist_metadata_path_clear, 'r', encoding='utf-8') as f:
                        artist_metadata_clear = json.load(f)
                    # Only clear if this is actually an artist images metadata file
                    if artist_metadata_clear.get("type") == "artist_images":
                        # Clear the preferred provider and filename to allow album art to be used
                        # CRITICAL FIX: Use = None instead of .pop() so save_album_db_metadata knows to delete them
                        # (pop() removes the keys, which causes save_album_db_metadata to restore them from existing metadata)
                        artist_metadata_clear["preferred_provider"] = None
                        artist_metadata_clear["preferred_image_filename"] = None
                        artist_metadata_clear["last_accessed"] = datetime.utcnow().isoformat() + "Z"
                        # Save the cleared metadata
                        save_album_db_metadata(artist_folder_clear, artist_metadata_clear)
                        logger.info(f"Cleared artist image preference when album art '{provider_name}' was selected")
                        
                        # CRITICAL FIX: Clear artist image cache to ensure album art is immediately shown
                        # When album art is selected, it overrides artist image preference, so we need to clear the cache
                        clear_artist_image_cache(artist)
                except (IOError, OSError, json.JSONDecodeError) as e:
                    # Expected errors - file issues or JSON parsing
                    logger.warning(f"Failed to clear artist image preference: {e}")
                except Exception as e:
                    # Unexpected error - log with traceback
                    logger.error(f"Unexpected error clearing artist image preference: {e}", exc_info=True)
            
            # Save updated metadata
            # CRITICAL FIX: Use title as fallback when album is missing (for singles)
            # This ensures we save to the correct folder: "Artist - Title" instead of just "Artist"
            folder = get_album_db_folder(artist, album_or_title)
            if not save_album_db_metadata(folder, db_metadata):
                return jsonify({"error": "Failed to save preference"}), 500
            
            # Log successful preference save for observability
            logger.info(f"Set album art preference to '{provider_name}' for {artist} - {album_or_title}")
            
            # Store provider data for use outside lock
            provider_data = providers[provider_name]
            filename = provider_data.get("filename", f"{provider_name}.jpg")
        
        # Copy selected image to cache for immediate use (preserving original format, outside lock to avoid blocking)
        db_image_path = folder / filename
    
    if db_image_path.exists():
        try:
            # Clean up old art first
            cleanup_old_art()
            
            # Get the original file extension from the DB image (preserves format)
            original_extension = db_image_path.suffix or '.jpg'
            
            # Copy DB image to cache with original extension (e.g., current_art.png, current_art.jpg)
            cache_path = CACHE_DIR / f"current_art{original_extension}"
            # FIX: Use unique temp filename to prevent concurrent writes from overwriting each other
            # This prevents race conditions when multiple preference updates happen simultaneously
            temp_filename = f"current_art_{uuid.uuid4().hex}{original_extension}.tmp"
            temp_path = CACHE_DIR / temp_filename
            
            shutil.copy2(db_image_path, temp_path)
            
            # Atomic replace with retry for Windows file locking (matching system_utils.py logic)
            # OPTIMIZATION: Use same lock (_art_update_lock) to prevent concurrent cache file updates
            # This ensures the cache file update is atomic with respect to other art operations (prevents flickering)
            # Note: This is a separate lock acquisition (not nested) since the metadata lock was released above
            # We keep file I/O outside the metadata lock to avoid blocking other metadata operations
            loop = asyncio.get_running_loop()
            async with _art_update_lock:
                replaced = False
                for attempt in range(3):
                    try:
                        import os
                        # Run blocking os.replace in executor to avoid blocking event loop
                        await loop.run_in_executor(None, os.replace, temp_path, cache_path)
                        replaced = True
                        break
                    except OSError:
                        if attempt < 2:
                            await asyncio.sleep(0.1)  # Wait briefly before retry
                        else:
                            logger.warning(f"Could not atomically replace current_art{original_extension} after 3 attempts (file may be locked)")
            
            # Clean up temp file if replacement failed
            if not replaced:
                try:
                    if temp_path.exists():
                        os.remove(temp_path)
                except:
                    pass
                return jsonify({"status": "error", "message": "Failed to update album art"})
            
            # OPTIMIZATION: Only delete spotify_art.jpg AFTER successful copy
            # This ensures we don't delete it if the copy failed, and prevents
            # aggressive deletion. server.py prefers spotify_art.jpg, so we delete
            # it to force fallback to our high-res current_art.*
            if replaced:
                spotify_art_path = CACHE_DIR / "spotify_art.jpg"
                if spotify_art_path.exists():
                    try:
                        os.remove(spotify_art_path)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Failed to copy selected art to cache: {e}")
    
    # CRITICAL FIX: Invalidate the metadata cache immediately!
    # This forces the server to reload the metadata (and thus the new art URL) on the next request.
    get_current_song_meta_data._last_check_time = 0
    # Also clear cached result to ensure fresh fetch
    if hasattr(get_current_song_meta_data, '_last_result'):
        get_current_song_meta_data._last_result = None
    
    # Add cache busting timestamp
    cache_bust = int(time.time())
    
    return jsonify({
        "status": "success",
        "message": f"Preferred provider set to {provider_name}",
        "provider": provider_name,
        "cache_bust": cache_bust
    })

@app.route("/api/album-art/preference", methods=['DELETE'])
async def clear_album_art_preference():
    """Clear BOTH album art and artist image preferences for current track"""
    from system_utils import get_current_song_meta_data, get_album_db_folder, save_album_db_metadata, _art_update_lock
    import json
    from datetime import datetime

    metadata = await get_current_song_meta_data()
    if not metadata:
        return jsonify({"error": "No song playing"}), 404

    artist = metadata.get("artist", "")
    album = metadata.get("album")
    title = metadata.get("title")
    album_or_title = album if album else title

    if not artist:
        return jsonify({"error": "Invalid song data"}), 400

    async with _art_update_lock:
        # 1. Clear Artist Image Preference
        try:
            artist_folder = get_album_db_folder(artist, None)
            artist_meta_path = artist_folder / "metadata.json"
            if artist_meta_path.exists():
                with open(artist_meta_path, 'r', encoding='utf-8') as f:
                    artist_data = json.load(f)
                
                if artist_data.get("type") == "artist_images":
                    # CRITICAL FIX: Explicitly set to None so save_album_db_metadata knows to delete it
                    # (pop() would be restored by safety logic in save function)
                    artist_data["preferred_provider"] = None
                    artist_data["preferred_image_filename"] = None
                    artist_data["last_accessed"] = datetime.utcnow().isoformat() + "Z"
                    save_album_db_metadata(artist_folder, artist_data)
                    logger.info(f"Cleared artist image preference for {artist}")
        except Exception as e:
            logger.error(f"Error clearing artist preference: {e}")

        # 2. Clear Album Art Preference
        if album_or_title:
            try:
                album_folder = get_album_db_folder(artist, album_or_title)
                album_meta_path = album_folder / "metadata.json"
                if album_meta_path.exists():
                    with open(album_meta_path, 'r', encoding='utf-8') as f:
                        album_data = json.load(f)
                    
                    # CRITICAL FIX: Explicitly set to None so save_album_db_metadata knows to delete it
                    # (pop() would be restored by safety logic in save function)
                    album_data["preferred_provider"] = None
                    # CRITICAL FIX: Also clear artist image preference from album folder
                    # This is stored as preferred_artist_image_filename (per-album behavior)
                    album_data["preferred_artist_image_filename"] = None
                    album_data["last_accessed"] = datetime.utcnow().isoformat() + "Z"
                    save_album_db_metadata(album_folder, album_data)
                    logger.info(f"Cleared album art and artist image preference for {artist} - {album_or_title}")
                    
                    # CRITICAL FIX: Clear artist image cache to ensure changes take effect immediately
                    clear_artist_image_cache(artist)
            except Exception as e:
                logger.error(f"Error clearing album art preference: {e}")

    # Invalidate cache
    get_current_song_meta_data._last_check_time = 0
    if hasattr(get_current_song_meta_data, '_last_result'):
        get_current_song_meta_data._last_result = None
    
    return jsonify({"status": "success", "message": "Art preferences cleared"})

@app.route("/api/album-art/background-style", methods=['POST'])
async def set_background_style():
    """Set preferred background style for current album (Sharp, Soft, Blur) - Phase 2"""
    from system_utils import get_current_song_meta_data, get_album_db_folder, load_album_art_from_db, save_album_db_metadata
    from datetime import datetime
    
    # Get current track info to know which album to update
    metadata = await get_current_song_meta_data()
    if not metadata:
        return jsonify({"error": "No song playing"}), 404
    
    data = await request.get_json()
    style = data.get('style')  # 'sharp', 'soft', 'blur', or 'none' to clear
    
    if not style:
        return jsonify({"error": "No style specified"}), 400
    
    # Validate style value
    if style not in ['sharp', 'soft', 'blur', 'none']:
        return jsonify({"error": f"Invalid style '{style}'. Must be 'sharp', 'soft', 'blur', or 'none'"}), 400
        
    artist = metadata.get("artist", "")
    album = metadata.get("album")
    title = metadata.get("title")  # Get title for fallback when album is missing (singles)
    
    if not artist:
        return jsonify({"error": "Invalid song data"}), 400
    
    # CRITICAL FIX: Use title as fallback when album is missing (for singles)
    # This ensures background styles work for singles, not just albums
    album_or_title = album if album else title
    
    if not album_or_title:
        return jsonify({"error": "Invalid song data: Missing album and title information"}), 400
    
    # Use lock to prevent race condition with background art download task
    # This ensures that if a background task is updating metadata, we don't overwrite each other
    from system_utils import _art_update_lock
    
    async with _art_update_lock:
        # Load existing metadata or create new if missing (though it should exist if art is there)
        # CRITICAL FIX: Pass album and title explicitly to match function signature
        # CRITICAL FIX: Use title fallback for singles support
        db_result = load_album_art_from_db(artist, album, title)
        
        if db_result:
            db_metadata = db_result["metadata"]
        else:
            # If no DB entry exists yet, we can't save preference easily without creating the structure
            # For now, return error if no art DB exists
            return jsonify({"error": "No album art database entry found. Please wait for art to download."}), 404
            
        # Update style (or remove if 'none')
        if style == 'none':
            # Explicitly set to None to signal deletion (save_album_db_metadata will filter this out)
            # This prevents the save function from restoring it from existing metadata
            db_metadata["background_style"] = None
            logger.info(f"Cleared background_style preference for {artist} - {album_or_title}")
        else:
            db_metadata["background_style"] = style
            logger.info(f"Set background_style to '{style}' for {artist} - {album_or_title}")
        db_metadata["last_accessed"] = datetime.utcnow().isoformat() + "Z"
        
        # Save
        # CRITICAL FIX: Use title fallback for singles support
        folder = get_album_db_folder(artist, album_or_title)
        if save_album_db_metadata(folder, db_metadata):
            # CRITICAL FIX: Invalidate metadata cache to force immediate reload of background_style
            # This ensures the "Auto" reset takes effect immediately in the UI
            get_current_song_meta_data._last_check_time = 0
            
            # FIX: Clear _last_result to invalidate audio recognition cache (stores background_style with _audio_rec_enriched flag)
            if hasattr(get_current_song_meta_data, '_last_result'):
                get_current_song_meta_data._last_result = None
            
            return jsonify({"status": "success", "style": style, "message": f"Saved {style} preference"})
        else:
            return jsonify({"error": "Failed to save preference"}), 500

@app.route("/api/album-art/image/<folder_name>/<filename>", methods=['GET'])
async def serve_album_art_image(folder_name: str, filename: str):
    """Serve album art images from database"""
    from config import ALBUM_ART_DB_DIR
    from quart import Response
    from urllib.parse import unquote
    import os
    
    try:
        # Decode URL-encoded folder name and filename
        decoded_folder = unquote(folder_name)
        decoded_filename = unquote(filename)
        
        # Build full path
        image_path = ALBUM_ART_DB_DIR / decoded_folder / decoded_filename
        
        # Security check: ensure path is within ALBUM_ART_DB_DIR
        try:
            image_path.resolve().relative_to(ALBUM_ART_DB_DIR.resolve())
        except ValueError:
            # Path outside ALBUM_ART_DB_DIR - security violation
            logger.warning(f"Security violation: Attempted to access path outside ALBUM_ART_DB_DIR: {image_path}")
            return "", 403
        
        if not image_path.exists():
            return "", 404
        
        # Read and serve image
        with open(image_path, 'rb') as f:
            image_data = f.read()
        
        # Determine mimetype based on file extension (preserves original format)
        ext = image_path.suffix.lower()
        mime = 'image/jpeg'  # Default
        if ext == '.png': mime = 'image/png'
        elif ext == '.bmp': mime = 'image/bmp'
        elif ext == '.gif': mime = 'image/gif'
        elif ext == '.webp': mime = 'image/webp'
        
        # Build cache headers with ETag/Last-Modified for efficient revalidation
        # After max-age expires (24h), browser validates with ETag → 304 if unchanged
        headers = {'Cache-Control': 'public, max-age=86400, must-revalidate'}
        
        try:
            # Last-Modified: file modification timestamp
            mtime = os.path.getmtime(str(image_path))
            from email.utils import formatdate
            headers['Last-Modified'] = formatdate(mtime, usegmt=True)
            
            # ETag: hash of path + mtime (fast, avoids hashing large image files)
            import hashlib
            etag_source = f"{image_path}:{mtime}".encode('utf-8')
            etag = hashlib.md5(etag_source).hexdigest()
            headers['ETag'] = f'"{etag}"'
        except Exception:
            # If mtime fails, just use max-age without ETag (still works)
            pass
        
        return Response(
            image_data,
            mimetype=mime,
            headers=headers
        )
    except Exception as e:
        logger.error(f"Error serving album art image: {e}")
        return "", 500

# --- Playback Control API (The New Features) ---

@app.route("/cover-art")
async def get_cover_art():
    """Serves the album art or background image directly from the source (DB or Thumbnail) without race conditions."""
    from system_utils import get_current_song_meta_data, get_cached_art_path
    from quart import send_file
    from pathlib import Path

    global _cover_art_log_throttle  # <--- CRITICAL FIX NEEDED HERE

    # 1. Get the current song metadata to find the real path
    metadata = await get_current_song_meta_data()
    
    # CRITICAL FIX: Check if this is a background image request (separate from album art display)
    # If type=background is in query params, serve background_image_path instead of album_art_path
    is_background = request.args.get('type') == 'background'
    
    # 2. Check if we have a direct path to the image (DB file or Unique Thumbnail)
    # For background: use background_image_path if available, otherwise fallback to album_art_path
    # For album art: always use album_art_path
    if metadata:
        if is_background and metadata.get("background_image_path"):
            art_path = Path(metadata["background_image_path"])
        elif metadata.get("album_art_path"):
            art_path = Path(metadata["album_art_path"])
        else:
            art_path = None
    else:
        art_path = None
    
    if art_path:
        # CRITICAL FIX: Verify file exists before serving (handles cleanup race conditions)
        # If thumbnail was deleted during cleanup while metadata cache still references it,
        # we fall through to legacy path instead of returning 404
        if art_path.exists():
            try:
                # DEBUG: Log size to verify quality
                file_size = art_path.stat().st_size
                
                # Throttle logging: only log once every 60 seconds per file
                # This prevents spam when frontend makes multiple simultaneous requests (main display, background, thumbnails, etc.)
                current_time = time.time()
                last_log_time = _cover_art_log_throttle.get(str(art_path), 0)
                if current_time - last_log_time > 60:
                    logger.info(f"Serving cover art: {art_path.name} ({file_size} bytes)")
                    _cover_art_log_throttle[str(art_path)] = current_time
                    
                    # Clean up old entries to prevent memory leak (keep only recent entries)
                    # Remove entries older than 5 minutes to prevent unbounded growth
                    if len(_cover_art_log_throttle) > 100:
                        cutoff_time = current_time - 300  # 5 minutes
                        _cover_art_log_throttle = {
                            k: v for k, v in _cover_art_log_throttle.items()
                            if v > cutoff_time
                        }
                
                # Determine mimetype based on extension (preserves original format)
                ext = art_path.suffix.lower()
                mime = 'image/jpeg'  # Default
                if ext == '.png': mime = 'image/png'
                elif ext == '.bmp': mime = 'image/bmp'
                elif ext == '.gif': mime = 'image/gif'
                elif ext == '.webp': mime = 'image/webp'
                
                # Serve the file directly with explicit no-cache headers
                # CRITICAL FIX: Explicit headers prevent browser caching issues
                response = await send_file(art_path, mimetype=mime)
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
                return response
            except Exception as e:
                logger.error(f"Failed to serve art from path {art_path}: {e}")
        else:
            # File was deleted (cleanup race condition), fall through to legacy path
            logger.debug(f"album_art_path {art_path} no longer exists, falling back to legacy path")

    # 3. Fallback to legacy current_art.jpg (only if no specific path found)
    # This ensures backward compatibility if metadata doesn't have album_art_path
    art_path = get_cached_art_path()
    if art_path and art_path.exists():
        try:
            # Determine mimetype based on extension (preserves original format)
            ext = art_path.suffix.lower()
            mime = 'image/jpeg'  # Default
            if ext == '.png': mime = 'image/png'
            elif ext == '.bmp': mime = 'image/bmp'
            elif ext == '.gif': mime = 'image/gif'
            elif ext == '.webp': mime = 'image/webp'
            
            # CRITICAL FIX: Explicit headers prevent browser caching issues
            response = await send_file(art_path, mimetype=mime)
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
        except (OSError, IOError) as e:
            logger.warning(f"Failed to read album art: {e}")
    
    return "", 404

async def _resolve_ma_player_id_for_request() -> Optional[str]:
    """Map ?player=<rtp-name> to the Music Assistant player_id to control.

    The player shown in the UI badge is the ONLY player controlled —
    no auto-detection of a different active player, no fallback to
    whatever happens to be playing.

    Priority:
      1. Registry music_assistant_player_id — set via the rename/link
         form in the player picker (authoritative).
      2. Registry display_name — users typically name their UDP stream
         to match the MA speaker label, so display_name IS the player_id.
      3. Addon-level music_assistant_player_id option (global fallback).

    Returns None when ?player= is absent or no mapping exists;
    the caller returns 500 so the user knows controls are not wired up.
    No MA WebSocket connection is needed for this resolution.
    """
    player_name = (request.args.get('player') or '').strip()
    if not player_name:
        return None

    try:
        from audio_recognition.player_registry import get_registry
        registry_cfg = get_registry().get(player_name)
        if registry_cfg:
            if registry_cfg.music_assistant_player_id:
                return registry_cfg.music_assistant_player_id
            display = (getattr(registry_cfg, 'display_name', '') or '').strip()
            if display:
                return display
    except Exception as exc:
        logger.debug(f"Player registry lookup failed for {player_name!r}: {exc}")

    configured_id = (conf("system.music_assistant.player_id", "") or "").strip()
    if configured_id:
        return configured_id

    logger.debug(
        "No MA player resolved for RTP player %r — link it via the player picker "
        "rename form or set music_assistant_player_id in addon options",
        player_name,
    )
    return None


async def _music_assistant_source_for_controls():
    from system_utils.sources.music_assistant import MusicAssistantSource
    target_ma_id = await _resolve_ma_player_id_for_request()
    rtp_player = (request.args.get('player') or '').strip() or '(none)'
    logger.info("MA controls: ?player=%r → resolved ma_player_id=%r", rtp_player, target_ma_id)
    return MusicAssistantSource(target_player_id=target_ma_id)


def _ma_failure_response(action: str, ma_source) -> tuple:
    """Build a 500 error response for a failed MA transport command.

    Returns an auth-specific error message when the failure was caused by a bad
    token, so the UX can show the user exactly what went wrong instead of a
    generic "playback control failed".
    """
    from system_utils.sources.music_assistant import _connected, _listening, is_configured, get_auth_error
    auth_err = get_auth_error()
    logger.warning(
        "MA %s FAILED — ma_configured=%s connected=%s listening=%s target=%r auth_error=%r",
        action, is_configured(), _connected, _listening, ma_source._target_player_id, auth_err,
    )
    msg = auth_err if auth_err else f"Music Assistant {action} failed"
    return jsonify({"error": msg}), 500


@app.route("/api/playback/play-pause", methods=['POST'])
async def toggle_playback():
    ma_source = await _music_assistant_source_for_controls()
    success = await ma_source.toggle_playback()
    if not success:
        return _ma_failure_response("play-pause", ma_source)
    return jsonify({"status": "success"})


@app.route("/api/playback/next", methods=['POST'])
async def next_track():
    ma_source = await _music_assistant_source_for_controls()
    success = await ma_source.next_track()
    if not success:
        return _ma_failure_response("next", ma_source)
    return jsonify({"status": "success"})


@app.route("/api/playback/previous", methods=['POST'])
async def previous_track():
    ma_source = await _music_assistant_source_for_controls()
    success = await ma_source.previous_track()
    if not success:
        return _ma_failure_response("previous", ma_source)
    return jsonify({"status": "success"})


@app.route("/api/playback/seek", methods=['POST'])
async def seek_playback():
    data = await request.get_json() or {}
    position_ms = data.get('position_ms')
    if position_ms is None:
        return jsonify({"error": "position_ms required"}), 400
    ma_source = await _music_assistant_source_for_controls()
    success = await ma_source.seek(position_ms)
    return jsonify({"status": "success", "position_ms": position_ms}) if success else (jsonify({"error": "Music Assistant seek failed"}), 500)


@app.route("/api/playback/queue", methods=['GET'])
async def get_playback_queue():
    ma_source = await _music_assistant_source_for_controls()
    queue_data = await ma_source.get_queue()
    return jsonify({
        "current": (queue_data or {}).get('current'),
        "queue": (queue_data or {}).get('queue', [])[:20],
        "source": "music_assistant"
    })


@app.route("/api/playback/queue/play-index", methods=['POST'])
async def play_queue_at_index():
    data = await request.get_json() or {}
    queue_index = data.get('queue_index')
    if queue_index is None:
        return jsonify({"error": "queue_index required"}), 400
    queue_item_id = data.get('queue_item_id') or None
    ma_source = await _music_assistant_source_for_controls()
    success = await ma_source.play_queue_item(int(queue_index), queue_item_id=queue_item_id)
    if not success:
        return _ma_failure_response("queue play-index", ma_source)
    return jsonify({"status": "success"})


@app.route("/api/playback/liked", methods=['GET'])
async def check_liked_status():
    track_id = request.args.get('track_id')
    if not track_id:
        return jsonify({"error": "No track_id provided"}), 400
    ma_source = await _music_assistant_source_for_controls()
    return jsonify({"liked": await ma_source.is_favorite(track_id)})


@app.route("/api/playback/liked", methods=['POST'])
async def toggle_liked_status():
    data = await request.get_json() or {}
    track_id = data.get('track_id')
    action = data.get('action')
    if not track_id or not action:
        return jsonify({"error": "Missing parameters"}), 400
    ma_source = await _music_assistant_source_for_controls()
    if action == 'like':
        success = await ma_source.add_to_favorites(track_id)
    elif action == 'unlike':
        success = await ma_source.remove_from_favorites(track_id)
    else:
        return jsonify({"error": "Invalid action"}), 400
    return jsonify({"success": success})


# ============================================================================
# Playback Controls API (Device Picker, Volume, Shuffle, Repeat)
# ============================================================================

@app.route("/api/spotify/devices", methods=['GET'])
async def get_spotify_devices():
    """Spotify Connect control is disabled in the UDP-only add-on."""
    return jsonify({"error": "Spotify app control is disabled in SyncLyricsUDP", "devices": []}), 410


@app.route("/api/spotify/transfer", methods=['POST'])
async def transfer_spotify_playback():
    """Spotify Connect control is disabled in the UDP-only add-on."""
    return jsonify({"error": "Spotify app control is disabled in SyncLyricsUDP"}), 410


# --- Generic Playback Device Routes (Auto-detect source) ---

@app.route("/api/playback/devices", methods=['GET'])
async def get_playback_devices():
    """Get list of available devices for current source.
    
    Query params:
        source: Optional. Force 'spotify' or 'music_assistant' instead of auto-detecting.
    
    Auto-detects source from current playback metadata and returns devices
    from either Music Assistant or Spotify.
    """
    # Check for forced source from query param
    forced_source = request.args.get('source')
    
    if forced_source:
        source = forced_source
    else:
        # Auto-detect from current playback
        metadata = await get_current_song_meta_data()
        source = metadata.get('source') if metadata else None
    
    if source == 'music_assistant':
        from system_utils.sources.music_assistant import MusicAssistantSource
        ma_source = MusicAssistantSource()
        devices = await ma_source.get_devices()
        return jsonify({"devices": devices, "source": "music_assistant"})
    return jsonify({"devices": [], "source": "udp", "message": "No playback device control for UDP input"})


@app.route("/api/playback/transfer", methods=['POST'])
async def transfer_playback():
    """Transfer playback to a specific device.
    
    Body: {"device_id": "...", "force_play": true}
    Routes to Music Assistant when available; UDP input itself has no device transfer.
    """
    data = await request.get_json()
    device_id = data.get('device_id')
    force_play = data.get('force_play', True)
    
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    
    metadata = await get_current_song_meta_data()
    source = metadata.get('source') if metadata else None
    
    if source == 'music_assistant':
        from system_utils.sources.music_assistant import MusicAssistantSource
        ma_source = MusicAssistantSource()
        success = await ma_source.transfer_playback(device_id)
        if success:
            return jsonify({"status": "success", "message": f"Transferred to {device_id}", "source": "music_assistant"})
        return jsonify({"error": "MA transfer failed"}), 500
    return jsonify({"error": "Playback transfer is only available for Music Assistant in the UDP-only add-on"}), 410


@app.route("/api/playback/volume", methods=['GET'])
async def get_volume():
    """Return Music Assistant volume for the selected/active player."""
    ma_source = await _music_assistant_source_for_controls()
    try:
        return jsonify({"music_assistant": await ma_source.get_volume()})
    except Exception as e:
        logger.debug(f"Could not get MA volume: {e}")
        return jsonify({})


@app.route("/api/playback/volume", methods=['POST'])
async def set_volume():
    """Set Music Assistant volume for the selected/active player.

    Body: {"source": "music_assistant", "volume": 0-100}
    """
    data = await request.get_json()
    volume = data.get('volume')
    if volume is None or not isinstance(volume, (int, float)):
        return jsonify({"error": "volume required (0-100)"}), 400
    volume = int(max(0, min(100, volume)))

    ma_source = await _music_assistant_source_for_controls()
    success = await ma_source.set_volume(volume)
    if success:
        return jsonify({"status": "success", "source": "music_assistant", "volume": volume})
    return jsonify({"error": "Failed to set MA volume"}), 500


@app.route("/api/playback/shuffle", methods=['POST'])
async def set_shuffle():
    data = await request.get_json() or {}
    ma_source = await _music_assistant_source_for_controls()
    if 'state' not in data:
        current_shuffle = await ma_source.get_shuffle()
        state = not current_shuffle if current_shuffle is not None else True
    else:
        state = bool(data.get('state'))
    success = await ma_source.set_shuffle(state)
    return jsonify({"status": "success", "shuffle": state, "source": "music_assistant"}) if success else (jsonify({"error": "Failed to set MA shuffle"}), 500)


@app.route("/api/playback/repeat", methods=['POST'])
async def set_repeat():
    data = await request.get_json() or {}
    ma_source = await _music_assistant_source_for_controls()
    if 'mode' not in data:
        current_repeat = await ma_source.get_repeat() or 'off'
        cycle = {'off': 'context', 'context': 'track', 'track': 'off'}
        mode = cycle.get(current_repeat, 'off')
    else:
        mode = data.get('mode')
        if mode not in ['off', 'context', 'track']:
            return jsonify({"error": "Invalid mode. Use: off, context, track"}), 400
    success = await ma_source.set_repeat(mode)
    return jsonify({"status": "success", "repeat": mode, "source": "music_assistant"}) if success else (jsonify({"error": "Failed to set MA repeat"}), 500)


# ============================================================================
# Audio Recognition API (UDP input)
# ============================================================================

@app.route('/api/audio-recognition/status', methods=['GET'])
async def audio_recognition_status():
    """Get UDP recognition status without initializing any local capture source."""
    mgr = _get_player_manager_if_running()
    if mgr is not None:
        engines = mgr.list_engines()
        live_engine = None
        for e in engines.values():
            if e.get_current_song():
                live_engine = e
                break

        max_audio_level = 0.0
        min_no_match = None
        engine_states = []
        for e in engines.values():
            try:
                st = e.get_status()
            except Exception:
                continue
            lvl = st.get("audio_level") or 0.0
            max_audio_level = max(max_audio_level, lvl)
            nm = st.get("consecutive_no_match")
            if nm is not None and (min_no_match is None or nm < min_no_match):
                min_no_match = nm
            engine_states.append({
                "player_name": st.get("player_name"),
                "state": st.get("state"),
                "is_playing": st.get("is_playing"),
                "audio_level": lvl,
                "consecutive_no_match": nm,
                "current_song": st.get("current_song"),
            })

        current_song = None
        if live_engine is not None:
            song = live_engine.get_current_song() or {}
            current_song = {
                "artist": song.get("artist"),
                "title": song.get("title"),
                "album": song.get("album"),
                "album_art_url": song.get("album_art_url"),
                "recognition_provider": song.get("recognition_provider", "shazam"),
            }
        return jsonify({
            "available": True,
            "enabled": True,
            "active": bool(engines),
            "running": bool(engines),
            "mode": "udp",
            "state": "listening" if engines else "idle",
            "udp_multi_instance": True,
            "player_count": len(engines),
            "reaper_detected": False,
            "auto_detect": False,
            "manual_mode": False,
            "capture_mode": "udp",
            "current_song": current_song,
            "audio_level": max_audio_level,
            "consecutive_no_match": min_no_match if min_no_match is not None else 0,
            "udp_mode": True,
            "engines": engine_states,
        })

    return jsonify({
        "available": True,
        "enabled": False,
        "active": False,
        "running": False,
        "mode": "udp",
        "state": "idle",
        "capture_mode": "udp",
        "udp_mode": True,
        "udp_only": True,
        "current_song": None,
        "message": "UDP listener is not running; check add-on recognition_enabled and UDP settings.",
    })


@app.route('/api/audio-recognition/start', methods=['POST'])
async def audio_recognition_start():
    """
    Start audio recognition manually.
    Body: {"manual": true} (optional, defaults to true for manual trigger)
    """
    # In multi-instance UDP mode the PlayerManager already owns port 6056;
    # letting the reaper engine start a second UDP listener just races and
    # fails with EADDRINUSE. Report success so the UI switches to "running".
    mgr = _get_player_manager_if_running()
    if mgr is not None:
        return jsonify({
            "status": "started",
            "mode": "udp",
            "udp_multi_instance": True,
            "message": "Recognition is already running via UDP multi-instance mode.",
        })

    return jsonify({
        "status": "udp_only",
        "mode": "udp",
        "message": "UDP recognition starts with the add-on; local capture sources are disabled.",
    })


@app.route('/api/audio-recognition/stop', methods=['POST'])
async def audio_recognition_stop():
    """Stop audio recognition."""
    # Refuse to tear down the shared PlayerManager from a reaper-style
    # "stop" click — it owns per-player engines and the UDP socket.
    mgr = _get_player_manager_if_running()
    if mgr is not None:
        return jsonify({
            "status": "running",
            "udp_multi_instance": True,
            "message": "UDP multi-instance mode is active; stop via addon config.",
        })

    return jsonify({
        "status": "udp_only",
        "mode": "udp",
        "message": "UDP recognition is managed by add-on startup configuration.",
    })


@app.route('/api/audio-recognition/devices', methods=['GET'])
async def audio_recognition_devices():
    """UDP-only compatibility endpoint: no local capture devices are exposed."""
    from config import UDP_AUDIO
    return jsonify({
        "devices": [{
            "id": "udp",
            "name": f"UDP audio on port {UDP_AUDIO.get('port', 6056)}",
            "is_udp": True,
            "sample_rate": UDP_AUDIO.get("sample_rate", 16000),
        }],
        "recommended": "udp",
        "count": 1,
        "udp_only": True,
    })


@app.route('/api/audio-recognition/config', methods=['GET'])
async def audio_recognition_get_config():
    """Return UDP-only recognition configuration."""
    from config import AUDIO_RECOGNITION, UDP_AUDIO
    mgr = _get_player_manager_if_running()
    status = {
        "active": bool(mgr and mgr.is_running),
        "running": bool(mgr and mgr.is_running),
        "mode": "udp",
        "capture_mode": "udp",
        "udp_only": True,
    }
    return jsonify({
        "config": {
            "enabled": AUDIO_RECOGNITION.get("enabled", True),
            "mode": "udp",
            "capture_duration": AUDIO_RECOGNITION.get("capture_duration", 6.0),
            "recognition_interval": AUDIO_RECOGNITION.get("recognition_interval", 4.0),
            "latency_offset": AUDIO_RECOGNITION.get("latency_offset", 0.0),
            "silence_threshold": AUDIO_RECOGNITION.get("silence_threshold", 350),
            "udp_port": UDP_AUDIO.get("port", 6056),
            "udp_sample_rate": UDP_AUDIO.get("sample_rate", 16000),
        },
        "status": status,
        "session_overrides_active": False,
        "active_overrides": {},
        "https_available": True,
        "udp_only": True,
    })


@app.route('/api/audio-recognition/configure', methods=['POST'])
async def audio_recognition_configure():
    """UDP-only compatibility endpoint; local input selection is disabled."""
    from config import AUDIO_RECOGNITION, UDP_AUDIO
    data = await request.get_json() or {}
    allowed_runtime = {"recognition_interval", "capture_duration", "latency_offset", "silence_threshold"}
    ignored = sorted(set(data) - allowed_runtime)
    return jsonify({
        "status": "udp_only",
        "config": {
            "enabled": AUDIO_RECOGNITION.get("enabled", True),
            "mode": "udp",
            "capture_duration": AUDIO_RECOGNITION.get("capture_duration", 6.0),
            "recognition_interval": AUDIO_RECOGNITION.get("recognition_interval", 4.0),
            "latency_offset": AUDIO_RECOGNITION.get("latency_offset", 0.0),
            "silence_threshold": AUDIO_RECOGNITION.get("silence_threshold", 350),
            "udp_port": UDP_AUDIO.get("port", 6056),
            "udp_sample_rate": UDP_AUDIO.get("sample_rate", 16000),
        },
        "active_overrides": {},
        "ignored_fields": ignored,
        "message": "UDP input is fixed; browser mic, local devices, and Reaper controls are disabled.",
    })


@app.websocket('/ws/audio-stream')
async def audio_stream_websocket():
    """Browser microphone streaming is disabled in the UDP-only add-on."""
    await websocket.close(1008, "Browser microphone input is disabled; send audio over UDP.")


# Spicetify bridge is intentionally not exposed in the UDP-only add-on.


# --- System Routes ---

@app.route('/settings', methods=['GET', 'POST'])
async def settings_page():
    if request.method == 'POST':
        form_data = await request.form
        errors = []
        changes_made = 0
        requires_restart = False
        
        # Legacy support
        theme = form_data.get('theme', 'dark')
        terminal = form_data.get('terminal-method', 'false').lower() == 'true'
        state = get_state()
        state = set_attribute_js_notation(state, 'theme', theme)
        state = set_attribute_js_notation(state, 'representationMethods.terminal', terminal)
        set_state(state)

        # New settings support
        for key, value in form_data.items():
            if key in ['theme', 'terminal-method']: continue
            try:
                # FIX: Use settings definitions for proper type conversion
                definition = settings._definitions.get(key)
                if definition:
                    if definition.type == bool:
                        val = value.lower() in ['true', 'on', '1', 'yes']
                    elif definition.type == int:
                        val = int(value) if value else definition.default
                    elif definition.type == float:
                        val = float(value) if value else definition.default
                    elif definition.type == list:
                        # Let validate_and_convert handle JSON/comma parsing
                        val = value  # Pass raw, settings.set will convert
                    else:
                        val = value
                else:
                    # Fallback for unknown keys
                    if value.lower() in ['true', 'on']: val = True
                    elif value.lower() in ['false', 'off']: val = False
                    elif value.isdigit(): val = int(value)
                    else: val = value
                
                setting_requires_restart = settings.set(key, val)
                if setting_requires_restart:
                    requires_restart = True
                changes_made += 1
            except Exception as e:
                logger.warning(f"Failed to set setting {key}: {e}")
                errors.append(f"{key}: {str(e)}")
        
        settings.save_to_config()
        
        # Flash messages for feedback
        if errors:
            await flash(f"Settings saved with {len(errors)} error(s): {', '.join(errors[:3])}", "warning")
        elif requires_restart:
            await flash("Settings saved! Some changes require a restart to take effect.", "info")
        else:
            await flash("Settings saved successfully!", "success")
        
        return redirect(url_for('settings_page'))

    # Render - organize settings with deprecated field
    settings_by_category = {}
    for key, setting in settings._definitions.items():
        cat = setting.category or "Misc"
        if cat not in settings_by_category: settings_by_category[cat] = {}
        settings_by_category[cat][key] = {
            'name': setting.name, 
            'type': setting.type.__name__,
            'value': settings.get(key), 
            'description': setting.description,
            'widget_type': setting.widget_type,
            'requires_restart': setting.requires_restart,
            'min_val': getattr(setting, 'min_val', None),
            'max_val': getattr(setting, 'max_val', None),
            'options': getattr(setting, 'options', None),
            'deprecated': getattr(setting, 'deprecated', False),
            'advanced': getattr(setting, 'advanced', False)
        }
    
    # Ensure 'Deprecated' category appears last in ordering
    ordered_settings = {}
    for cat in sorted(settings_by_category.keys(), key=lambda x: (x == 'Deprecated', x)):
        ordered_settings[cat] = settings_by_category[cat]
    
    return await render_template('settings.html', settings=ordered_settings, theme=get_attribute_js_notation(get_state(), 'theme'))

@app.route('/reset-defaults')
async def reset_defaults():
    settings.reset_to_defaults()
    await flash("All settings have been reset to defaults.", "info")
    return redirect(url_for('settings_page'))

@app.route("/exit-application")
async def exit_application() -> dict:
    from context import queue
    from sync_lyrics import force_exit
    queue.put("exit")
    import threading
    threading.Timer(2.0, force_exit).start()
    return {"status": "ok"}, 200

@app.route("/restart", methods=['POST'])
async def restart_server():
    from context import queue
    queue.put("restart")
    return {'status': 'ok'}, 200

@app.route('/config')
async def get_client_config():
    # Get custom font names for dropdown
    from font_scanner import get_custom_font_names
    custom_fonts = get_custom_font_names(RESOURCES_DIR / "fonts")
    
    return {
        "updateInterval": LYRICS["display"]["update_interval"] * 1000,
        "blurStrength": settings.get("ui.blur_strength"),
        "overlayOpacity": settings.get("ui.overlay_opacity"),
        "sharpAlbumArt": settings.get("ui.sharp_album_art"),
        "softAlbumArt": settings.get("ui.soft_album_art"),
        # Visual Mode settings
        "visualModeEnabled": settings.get("visual_mode.enabled"),
        "visualModeDelaySeconds": settings.get("visual_mode.delay_seconds"),
        "visualModeAutoSharp": settings.get("visual_mode.auto_sharp"),
        "slideshowEnabled": settings.get("visual_mode.slideshow.enabled"),
        "slideshowIntervalSeconds": settings.get("visual_mode.slideshow.interval_seconds"),
        # Slideshow (Art Cycling) settings
        "slideshowDefaultEnabled": settings.get("slideshow.default_enabled"),
        "slideshowConfigIntervalSeconds": settings.get("slideshow.interval_seconds"),
        "slideshowKenBurnsEnabled": settings.get("slideshow.ken_burns_enabled"),
        "slideshowKenBurnsIntensity": settings.get("slideshow.ken_burns_intensity"),
        "slideshowShuffle": settings.get("slideshow.shuffle"),
        "slideshowTransitionDuration": settings.get("slideshow.transition_duration"),
        # Word-sync settings
        "word_sync_default_enabled": settings.get("features.word_sync_default_enabled", True),
        "wordSyncTransitionMs": settings.get("lyrics.display.word_sync_transition_ms", 0),
        # Lyrics font size multipliers
        "lyricsFontSizeCurrent": settings.get("lyrics.display.font_size_current"),
        "lyricsFontSizeAdjacent": settings.get("lyrics.display.font_size_adjacent"),
        "lyricsFontSizeFar": settings.get("lyrics.display.font_size_far"),
        "lyricsFontSizeMobile": settings.get("lyrics.display.font_size_mobile"),
        # Font and styling settings
        "lyricsFontFamily": settings.get("lyrics.font_family"),
        "lyricsGlowIntensity": settings.get("lyrics.glow_intensity"),
        "lyricsTextColor": settings.get("lyrics.text_color"),
        "lyricsFontWeight": settings.get("lyrics.font_weight"),
        "uiFontFamily": settings.get("ui.font_family"),
        # Custom fonts for dropdown
        "customFonts": custom_fonts,
        # Pixel scroll settings
        "pixelScrollEnabled": settings.get("lyrics.display.pixel_scroll_enabled", False),
        "pixelScrollSpeed": settings.get("lyrics.display.pixel_scroll_speed", 1.0),
    }

@app.route("/callback")
async def spotify_callback():
    """Spotify OAuth is disabled in the UDP-only add-on."""
    return "Spotify app control/OAuth is disabled in SyncLyricsUDP.", 410


@app.route('/media-browser/')
@app.route('/media-browser/<path:subpath>')
async def media_browser(subpath='index.html'):
    """
    Serves the media browser.
    - For Spotify: serves static React client files from resources/spotify-browser
    - For MA: returns page with iframe to user's MA server URL
    
    Query params:
    - source: 'spotify' (default) or 'music_assistant'  
    - token: Spotify access token (for Spotify source)
    """
    source = request.args.get('source', 'spotify')
    
    if source == 'music_assistant':
        # Get MA server URL from config (checks env vars first, then settings.json)
        ma_url = conf('system.music_assistant.server_url', '')
        if not ma_url:
            return """
            <html>
            <head><title>Music Assistant Not Configured</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px; background: #1a1a2e; color: #fff;">
                <h1>⚠️ Music Assistant Not Configured</h1>
                <p>Please configure the Music Assistant server URL in Settings or .env file.</p>
                <p><code>SYSTEM_MUSIC_ASSISTANT_SERVER_URL=http://your-ma-server:8095</code></p>
            </body>
            </html>
            """, 400
        
        # Get MA token for auto-authentication (optional)
        ma_token = conf('system.music_assistant.token', '')
        
        # Build iframe URL with optional ?code= parameter for auto-auth
        iframe_url = ma_url
        if ma_token:
            # MA uses ?code= for long-lived token auth
            separator = '&' if '?' in ma_url else '?'
            iframe_url = f"{ma_url}{separator}code={ma_token}"
        
        # Return a simple page that iframes the MA server
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Music Assistant</title>
            <style>
                body, html {{ margin: 0; padding: 0; height: 100%; overflow: hidden; background: #1a1a2e; }}
                iframe {{ width: 100%; height: 100%; border: none; }}
            </style>
        </head>
        <body>
            <iframe src="{iframe_url}" allow="autoplay"></iframe>
        </body>
        </html>
        """
    else:
        # Serve Spotify React client static files
        spotify_browser_dir = RESOURCES_DIR / "spotify-browser"
        
        # Handle the root path - serve index.html
        if subpath == '' or subpath == 'index.html':
            subpath = 'index.html'
        
        # CRITICAL: React build uses /static/ paths which conflict with SyncLyrics' own /static/ route
        # We need to serve static files from the spotify-browser directory
        return await send_from_directory(str(spotify_browser_dir), subpath)


@app.route('/api/spotify/browser-token')
async def get_spotify_browser_token():
    """Spotify app browser integration is disabled in the UDP-only add-on."""
    return jsonify({"error": "Spotify app integration is disabled in SyncLyricsUDP"}), 410


@app.route("/api/artist/images", methods=['GET'])
async def get_artist_images():
    """Get artist images from local DB, optionally including metadata and slideshow preferences."""
    artist_id = request.args.get('artist_id')
    artist_name_param = request.args.get('artist_name')
    include_metadata = request.args.get('include_metadata', 'false').lower() == 'true'
    player_scope = _player_name_from_request()

    hint_token = None
    if player_scope:
        hint_token = system_state.metadata_player_hint.set(player_scope)
    try:
        metadata = await get_current_song_meta_data()
    finally:
        if hint_token is not None:
            system_state.metadata_player_hint.reset(hint_token)

    # Use explicit frontend artist_name if provided (bypasses lagging recognition metadata)
    artist_name = artist_name_param or (metadata.get('artist') if metadata else None)
    
    if not artist_name:
        return jsonify({"images": [], "count": 0, "artist_name": None})

    if metadata and metadata.get('artist_id') and not artist_id:
        artist_id = metadata.get('artist_id')

    from system_utils import ensure_artist_image_db
    images = await ensure_artist_image_db(artist_name, artist_id)

    response = {
        "artist_id": artist_id,
        "artist_name": artist_name,
        "images": images,
        "count": len(images),
    }

    if include_metadata:
        from system_utils.artist_image import get_slideshow_preferences
        folder = get_album_db_folder(artist_name, None)
        metadata_path = folder / "metadata.json"
        image_metadata = []
        if metadata_path.exists():
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    full_metadata = json.load(f)
                for img in full_metadata.get("images", []):
                    if img.get("downloaded") and img.get("filename"):
                        image_metadata.append({
                            "source": img.get("source", "unknown"),
                            "filename": img.get("filename"),
                            "width": img.get("width"),
                            "height": img.get("height"),
                            "added_at": img.get("added_at"),
                        })
            except Exception as e:
                logger.debug(f"Failed to load image metadata for '{artist_name}': {e}")
        response["metadata"] = image_metadata
        response["preferences"] = get_slideshow_preferences(artist_name)

    return jsonify(response)


@app.route("/api/artist/images/preferences", methods=['POST'])
async def save_artist_slideshow_preferences_endpoint():
    """Save slideshow preferences (excluded images, auto-enable, favorites) for an artist."""
    data = await request.get_json() or {}
    artist = data.get('artist')
    if not artist:
        return jsonify({"error": "Artist name required"}), 400
    from system_utils.artist_image import save_slideshow_preferences
    preferences = {
        "excluded": data.get('excluded', []),
        "auto_enable": data.get('auto_enable'),
        "favorites": data.get('favorites', []),
    }
    success = save_slideshow_preferences(artist, preferences)
    return jsonify({"success": success})


@app.route('/api/slideshow/random-images')
async def get_random_slideshow_images():
    """
    Get a random selection of images from the global album art database.
    Used for the idle screen dashboard.
    """
    try:
        limit = int(request.args.get('limit', 20))
        current_time = time.time()
        
        # Check cache validity
        if not _slideshow_cache['images'] or (current_time - _slideshow_cache['last_update'] > _SLIDESHOW_CACHE_TTL):
            logger.info("Refeshing slideshow image cache...")
            
            # Helper to recursively find images
            def find_all_images():
                images = []
                if not ALBUM_ART_DB_DIR.exists():
                    return []
                    
                # Walk through the database
                for root, _, files in os.walk(ALBUM_ART_DB_DIR):
                    for file in files:
                        if file.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp')):
                            # Get relative path from DB root for the API URL
                            full_path = Path(root) / file
                            try:
                                rel_path = full_path.relative_to(ALBUM_ART_DB_DIR)
                                # Convert Windows path separators to forward slashes for URL
                                url_path = str(rel_path).replace('\\', '/')
                                images.append(f"/api/album-art/image/{url_path}")
                            except ValueError:
                                pass
                return images

            # Run file scan in thread to avoid blocking
            loop = asyncio.get_running_loop()
            all_images = await loop.run_in_executor(None, find_all_images)
            
            # Update cache
            if all_images:
                _slideshow_cache['images'] = all_images
                _slideshow_cache['last_update'] = current_time
                logger.info(f"Slideshow cache updated with {len(all_images)} images")
        
        # Use cached images
        all_images = _slideshow_cache['images']
        
        if not all_images:
            return jsonify({'images': []})
            
        # Shuffle and pick random subset (from cache)
        # We copy the list to avoid modifying the cache with shuffle
        shuffled = all_images.copy()
        random.shuffle(shuffled)
        selected_images = shuffled[:limit]
        
        return jsonify({
            'images': selected_images,
            'total_available': len(all_images)
        })
        
    except Exception as e:
        logger.error(f"Error generating random slideshow: {e}")
        return jsonify({'error': str(e)}), 500