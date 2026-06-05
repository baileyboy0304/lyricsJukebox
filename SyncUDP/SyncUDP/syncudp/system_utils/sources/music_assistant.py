"""
Music Assistant metadata source plugin.

This plugin provides metadata from Music Assistant (MA), a music server
commonly used with Home Assistant. It uses WebSockets for real-time
updates and supports full playback controls.

Requirements:
- Music Assistant server (standalone or Home Assistant add-on)
- API token (generate in MA web UI)

Features:
- Real-time metadata via WebSocket
- Playback controls (play, pause, next, previous, seek)
- Queue support
- Auto-reconnection with exponential backoff
- Multi-player support (auto-detect or user-specified)
"""
import asyncio
import time
import logging
from typing import Optional, Dict, Any, List
from .base import BaseMetadataSource, SourceConfig, SourceCapability
from ..helpers import _normalize_track_id
from logging_config import get_logger

logger = get_logger(__name__)

# SECURITY: Suppress MA client loggers that log sensitive data (tokens, full messages)
# The connection logger logs full WebSocket messages including auth tokens at DEBUG level
logging.getLogger("music_assistant_client.connection").setLevel(logging.WARNING)
logging.getLogger("music_assistant_client").setLevel(logging.WARNING)


# Connection state
_client = None
_connection_task = None
_connected = False
_listening = False
_last_connect_attempt = 0
_reconnect_delay = 1  # Start at 1 second, exponential backoff

# Background connection management (non-blocking)
_connection_lock: Optional[asyncio.Lock] = None  # Created lazily for event loop safety
_connecting = False  # Fast check to avoid duplicate connection tasks
_last_group_resolve = None  # Last logged group-resolution result (dedupe spam)
_last_source_select_log = None  # Last logged (active_source, queue_id, external) tuple
_listener_task: Optional[asyncio.Task] = None  # Track listener to prevent duplicates

# State cache (updated by WebSocket events)
_current_player_id: Optional[str] = None
_current_queue_id: Optional[str] = None
_last_active_time: float = 0
_last_active_player_id: Optional[str] = None  # Track player that was last playing/paused
_metadata_cache: Optional[Dict[str, Any]] = None
_cache_time: float = 0

# Log rate limiting - prevent spam in logs
_last_no_player_log: float = 0
_last_player_not_found_log: float = 0
_last_queue_error_log: float = 0
_last_metadata_error_log: float = 0
_last_disconnect_log: float = 0
_last_selected_player_state_log: Dict[str, tuple] = {}
_connection_attempt_count: int = 0  # Track consecutive connection attempts
LOG_THROTTLE_INTERVAL = 60.0  # Only log repeated messages once per minute

# Favorites cache - avoids repeated API calls for same track
_favorite_cache: Dict[str, bool] = {}  # item_id -> is_favorite
_favorite_cache_time: Dict[str, float] = {}  # item_id -> timestamp
FAVORITE_CACHE_TTL = 30.0  # Cache favorite status for 30 seconds

# Constants
MAX_RECONNECT_DELAY = 60  # Max 60 seconds between reconnection attempts
CACHE_TTL = 1.0  # Cache TTL in seconds (MA updates come via events)

# Auth error tracking — set when connection fails due to bad token/credentials
_auth_error: Optional[str] = None


def _state_str(obj, default: str = "idle") -> str:
    """Safely convert a playback_state/queue.state to a lowercase string.

    MA delivers these as either an enum (with .value) or a plain string.
    Calling .value on a plain string raises AttributeError, which the outer
    try/except in get_metadata() silently catches and returns None — masking
    the real state.  This helper handles both forms safely.
    """
    if not obj:
        return default
    return (obj.value if hasattr(obj, "value") else str(obj)).lower()


def get_auth_error() -> Optional[str]:
    """Return the last authentication error message, or None if no auth error."""
    return _auth_error


def clear_auth_error():
    """Clear any stored auth error (called on successful connection)."""
    global _auth_error
    _auth_error = None


def _get_config_value(key: str, default: Any = None) -> Any:
    """Get config value with proper type handling."""
    from config import conf
    return conf(key, default)


def is_configured() -> bool:
    """Check if Music Assistant is configured (server URL provided)."""
    server_url = _get_config_value("system.music_assistant.server_url", "")
    return bool(server_url and server_url.strip())


def is_connected() -> bool:
    """Check if connected to Music Assistant server."""
    return _connected and _client is not None


def is_ready() -> bool:
    """Check if MA is connected and listening (non-blocking)."""
    return _connected and _client is not None and _listening


def _get_connection_lock() -> asyncio.Lock:
    """Get or create the connection lock (lazy init for event loop safety)."""
    global _connection_lock
    if _connection_lock is None:
        _connection_lock = asyncio.Lock()
    return _connection_lock


async def _connect() -> bool:
    """
    Connect to Music Assistant server.

    Uses exponential backoff for reconnection attempts.
    Returns True if connected, False otherwise.
    """
    global _client, _connected, _listening, _last_connect_attempt, _reconnect_delay
    global _connection_attempt_count, _auth_error

    # Only require WebSocket open (_connected + _client), not _listening.
    # _listening is set asynchronously by the start_listening task and must
    # not gate basic connectivity checks — doing so causes rate-limit loops.
    if _connected and _client is not None:
        return True
    
    # Check if configured
    if not is_configured():
        logger.warning("MA not configured — set music_assistant_base_url in addon options")
        return False

    # Rate limit connection attempts (background path only;
    # _ensure_connected resets _last_connect_attempt before calling us)
    now = time.time()
    if now - _last_connect_attempt < _reconnect_delay:
        logger.warning(
            "MA connection rate-limited: %.0fs until next attempt (backoff=%ds)",
            _reconnect_delay - (now - _last_connect_attempt),
            _reconnect_delay,
        )
        return False
    
    _last_connect_attempt = now
    _connection_attempt_count += 1
    
    server_url = _get_config_value("system.music_assistant.server_url", "")
    token = _get_config_value("system.music_assistant.token", "")
    
    try:
        from music_assistant_client import MusicAssistantClient
        
        # Log INFO on first attempt, DEBUG on retries to reduce spam
        if _connection_attempt_count == 1:
            logger.info(f"Connecting to Music Assistant: {server_url}")
        else:
            logger.debug(f"Reconnecting to Music Assistant (attempt {_connection_attempt_count})")
        
        # Create client (token may be optional for older schema versions)
        _client = MusicAssistantClient(
            server_url=server_url,
            aiohttp_session=None,
            token=token if token else None,
        )
        
        # Connect with timeout
        await asyncio.wait_for(_client.connect(), timeout=4.0)
        
        _connected = True
        _reconnect_delay = 1  # Reset backoff on success
        _connection_attempt_count = 0  # Reset on success
        _auth_error = None  # Clear any previous auth error on successful connection

        logger.info("Connected to Music Assistant")
        
        # Start listening in background to receive player/queue updates
        # This populates _client.players.players and _client.player_queues.player_queues
        # Cancel any existing listener first to prevent duplicates
        global _listener_task
        if _listener_task and not _listener_task.done():
            _listener_task.cancel()
        _listener_task = asyncio.create_task(_start_listening())
        
        return True
        
    except ImportError:
        logger.error("music-assistant-client not installed. Run: pip install music-assistant-client")
        _reconnect_delay = MAX_RECONNECT_DELAY  # Don't retry frequently
        return False
    except asyncio.TimeoutError:
        logger.warning(
            "MA connection timed out (url=%r attempt=%d backoff now %ds)",
            _get_config_value("system.music_assistant.server_url", ""),
            _connection_attempt_count,
            min(_reconnect_delay * 2, MAX_RECONNECT_DELAY),
        )
        _auth_error = None
        _reconnect_delay = min(_reconnect_delay * 2, MAX_RECONNECT_DELAY)
        await _cleanup_failed_client()
        return False
    except Exception as e:
        # Detect authentication failures (bad token / wrong credentials)
        error_str = str(e)
        error_lower = error_str.lower()
        is_auth_error = False
        try:
            import aiohttp
            if isinstance(e, aiohttp.ClientResponseError) and e.status in (401, 403):
                is_auth_error = True
        except ImportError:
            pass
        if not is_auth_error and any(
            kw in error_lower
            for kw in ("401", "403", "unauthorized", "forbidden", "invalid token", "authentication")
        ):
            is_auth_error = True

        if is_auth_error:
            _auth_error = (
                "Music Assistant: Authentication failed — check your API token in addon settings"
            )
            logger.warning("MA authentication error (url=%r): %s", _get_config_value("system.music_assistant.server_url", ""), e)
        else:
            _auth_error = None
            logger.warning(
                "MA connection failed (url=%r attempt=%d): %s",
                _get_config_value("system.music_assistant.server_url", ""),
                _connection_attempt_count,
                e,
            )
        _reconnect_delay = min(_reconnect_delay * 2, MAX_RECONNECT_DELAY)
        await _cleanup_failed_client()
        return False


async def _cleanup_failed_client():
    """Clean up client resources after failed connection."""
    global _client, _connected, _listening
    
    if _client:
        try:
            # Properly close the client to avoid unclosed session warnings
            await _client.disconnect()
        except Exception:
            pass
    
    _client = None
    _connected = False
    _listening = False


async def _start_listening():
    """
    Start the WebSocket listener to receive player/queue events.
    
    This runs in the background and keeps the player list updated.
    """
    global _listening, _connected, _client, _last_disconnect_log
    
    if not _client:
        return
    
    try:
        _listening = True
        logger.debug("Starting Music Assistant event listener")
        await _client.start_listening()
    except Exception as e:
        logger.debug(f"Music Assistant listener stopped: {e}")
    finally:
        _listening = False
        _connected = False
        # Rate limit disconnected log to prevent spam on reconnect cycles
        now = time.time()
        if now - _last_disconnect_log >= LOG_THROTTLE_INTERVAL:
            logger.info("Music Assistant disconnected")
            _last_disconnect_log = now


async def _wait_for_ready(timeout: float = 3.0) -> bool:
    """Wait for MA to finish the start_listening handshake (non-blocking poll).

    Useful after _ensure_connected() returns True but _listening is still
    False because the listener task hasn't had a scheduling slot yet.
    Returns True once is_ready(), or False after *timeout* seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_ready():
            return True
        await asyncio.sleep(0.05)
    return is_ready()


async def _ensure_connected() -> bool:
    """Ensure we're connected; bypasses backoff rate-limit for explicit commands.

    Background polling uses _ensure_connected_nonblocking() which respects
    the rate limit.  Transport controls call this and must always attempt
    a real connection so a button press never silently fails due to backoff.
    """
    if _connected and _client is not None:
        return True
    # Reset the rate-limit window so _connect() always runs for explicit commands.
    global _last_connect_attempt
    _last_connect_attempt = 0
    result = await _connect()
    if not result:
        server_url = _get_config_value("system.music_assistant.server_url", "") or "(not set)"
        logger.warning(
            "MA _ensure_connected: failed — url=%r configured=%s connected=%s",
            server_url, is_configured(), _connected,
        )
    return result


async def _ensure_connected_nonblocking() -> bool:
    """
    Check connection, trigger background reconnect if needed.
    
    NEVER BLOCKS - returns immediately.
    If not connected, schedules background connection task.
    Use this in get_metadata() and frequent poll paths.
    """
    global _connection_task, _connecting
    
    if is_ready():
        return True
    
    # Not connected - trigger background connection if not already running
    if not _connecting and (_connection_task is None or _connection_task.done()):
        _connecting = True
        _connection_task = asyncio.create_task(_background_connect())
    
    return False  # Not ready yet - caller should return None


async def _background_connect():
    """
    Background connection task with exponential backoff.
    
    Runs until connected or cancelled. Uses lock to prevent
    multiple simultaneous connection attempts.
    """
    global _connecting
    
    async with _get_connection_lock():
        try:
            while not _connected:
                success = await _connect()
                if success:
                    break
                
                # Wait before retry (exponential backoff already handled in _connect)
                await asyncio.sleep(_reconnect_delay)
        except asyncio.CancelledError:
            logger.debug("Background connection task cancelled")
        finally:
            _connecting = False


def _get_target_player_id() -> Optional[str]:
    """
    Get the player ID to monitor.
    
    Priority:
    1. User-configured player_id setting
    2. First player with state PLAYING
    3. First player with state PAUSED (recently active)
    4. Last active player (if still exists)
    5. First available player
    """
    global _current_player_id, _last_active_player_id
    
    if not _client:
        return None
    
    # Check user preference
    preferred_id = _get_config_value("system.music_assistant.player_id", "")
    if preferred_id and preferred_id.strip():
        player = _client.players.get(preferred_id.strip())
        if player:
            return player.player_id
        # Player not in the in-memory cache yet (start_listening still syncing).
        # Return the configured ID directly so control commands can proceed —
        # the WebSocket is open and MA will handle the command.
        if not _listening:
            return preferred_id.strip()
        # Cache populated but player not found — log once per minute
        global _last_player_not_found_log
        now = time.time()
        if now - _last_player_not_found_log >= LOG_THROTTLE_INTERVAL:
            logger.debug(f"Configured player_id '{preferred_id}' not found")
            _last_player_not_found_log = now
    
    # Find first playing player
    for player in _client.players.players:
        if _state_str(player.playback_state) == "playing":
            _last_active_player_id = player.player_id  # Remember active player
            return player.player_id

    # Find first paused player (recently active)
    for player in _client.players.players:
        if _state_str(player.playback_state) == "paused":
            return player.player_id
    
    # Use last active player if still exists
    if _last_active_player_id:
        player = _client.players.get(_last_active_player_id)
        if player:
            return player.player_id
    
    # Fall back to first available player
    players = list(_client.players.players)
    if players:
        return players[0].player_id
    
    return None


def _resolve_group_target_id(player_id: str) -> str:
    """Resolve a player to the id that actually owns its active queue.

    Two redirects are needed, in order:

    1. **Protocol player → Universal Player.** SyncLyrics binds to the Sendspin
       *protocol* player id (a UUID/MAC, e.g. ``a0505ec6-...``). That object has
       no usable queue in MA (``queue_not_cached`` / ``target_state=missing``).
       The queue-owning, groupable player is the *Universal Player* that wraps
       it, whose id is ``"up"`` + the protocol id with separators removed
       (``a0505ec6-... → upa0505ec6...``). Prefer the wrapper when it exists.

    2. **Universal Player → sync group / leader.** A player that is part of a
       sync group has no usable queue of its own — the active queue lives on the
       group/leader, exposed via ``Player.active_group`` then ``synced_to``.

    Skipping either step is why the transport icon stuck on paused and why
    play/pause failed with "Queue <id> is not available".
    """
    global _last_group_resolve
    if not _client:
        return player_id

    # Step 1: protocol id → Universal Player wrapper.
    resolved = player_id
    universal_id = "up" + player_id.replace("-", "").replace(":", "").lower()
    if _client.players.get(universal_id) is not None:
        resolved = universal_id

    # Step 2: follow the sync group / leader to the queue-owning coordinator.
    player = _client.players.get(resolved)
    if player is not None:
        target = (
            getattr(player, "active_group", None)
            or getattr(player, "synced_to", None)
        )
        if target and target != resolved:
            resolved = target

    if resolved != player_id:
        msg = "MA group resolve: %r → %r" % (player_id, resolved)
        if msg != _last_group_resolve:
            _last_group_resolve = msg
            logger.info(msg)
    return resolved


async def _get_active_queue_id(player_id: str) -> Optional[str]:
    """Get the active queue ID for a player (following any sync group)."""
    global _current_queue_id

    if not _client:
        return None

    # Follow sync-group / sync-leader so we target the queue that is actually
    # active rather than the follower's idle own-queue.
    player_id = _resolve_group_target_id(player_id)

    try:
        queue = await _client.player_queues.get_active_queue(player_id)
        if queue:
            _current_queue_id = queue.queue_id
            return queue.queue_id
    except Exception as e:
        # Rate limit queue error log
        global _last_queue_error_log
        now = time.time()
        if now - _last_queue_error_log >= LOG_THROTTLE_INTERVAL:
            logger.debug(f"Failed to get active queue: {e}")
            _last_queue_error_log = now
    
    # Fallback: queue_id often equals player_id
    return player_id


def _player_label(player) -> Optional[str]:
    """Return the most useful human-readable label for an MA player."""
    if not player:
        return None
    for attr in ("display_name", "name", "player_id"):
        value = getattr(player, attr, None)
        if value:
            return str(value)
    return None


def _current_item_label(queue) -> Optional[str]:
    """Return a compact label for the queue's current item for state logs."""
    item = getattr(queue, "current_item", None) if queue else None
    if not item:
        return None

    media_item = getattr(item, "media_item", None)
    if media_item:
        title = getattr(media_item, "name", None) or getattr(item, "name", None)
        artist = ""
        artists = getattr(media_item, "artists", None)
        if artists:
            artist = getattr(artists[0], "name", "") or ""
        elif getattr(media_item, "artist", None):
            artist = str(media_item.artist)
        if artist and title:
            return f"{artist} - {title}"
        if title:
            return str(title)

    return str(
        getattr(item, "name", None)
        or getattr(item, "queue_item_id", None)
        or "<unknown item>"
    )


def _log_selected_player_state_change(
    *,
    target_id: Optional[str],
    target_player,
    queue_id: Optional[str],
    queue,
    coordinator_player,
    is_playing: Optional[bool],
    reason: str,
) -> None:
    """Log selected MA player/coordinator state whenever it changes.

    The selected target player can differ from the player that owns the active
    queue during sync-group playback. Logging both sides makes it clear whether
    a child is idle/paused while its coordinator queue is still playing.
    """
    global _last_selected_player_state_log

    target_state = (
        _state_str(getattr(target_player, "playback_state", None), "missing")
        if target_player else "missing"
    )
    queue_state = _state_str(getattr(queue, "state", None), "missing") if queue else "missing"
    coordinator_state = (
        _state_str(getattr(coordinator_player, "playback_state", None), "missing")
        if coordinator_player else "missing"
    )
    coordinator_id = getattr(coordinator_player, "player_id", None) if coordinator_player else None
    has_current_item = bool(getattr(queue, "current_item", None)) if queue else False
    current_item = _current_item_label(queue)
    elapsed_time = getattr(queue, "elapsed_time", None) if queue else None
    corrected_elapsed_time = getattr(queue, "corrected_elapsed_time", None) if queue else None
    updated_at = getattr(queue, "elapsed_time_last_updated", None) if queue else None
    age = round(time.time() - updated_at, 3) if updated_at else None

    cache_key = target_id or "<auto>"
    state_key = (
        target_id,
        target_state,
        queue_id,
        queue_state,
        coordinator_id,
        coordinator_state,
        is_playing,
        reason,
        has_current_item,
        current_item,
    )
    if _last_selected_player_state_log.get(cache_key) == state_key:
        return
    _last_selected_player_state_log[cache_key] = state_key

    logger.info(
        "MA selected-player state changed: target_id=%r target_name=%r "
        "target_state=%s queue_id=%r queue_state=%s coordinator_id=%r "
        "coordinator_name=%r coordinator_state=%s is_playing=%s reason=%s "
        "has_current_item=%s item=%r elapsed=%r corrected_elapsed=%r elapsed_age_s=%r",
        target_id,
        _player_label(target_player),
        target_state,
        queue_id,
        queue_state,
        coordinator_id,
        _player_label(coordinator_player),
        coordinator_state,
        is_playing,
        reason,
        has_current_item,
        current_item,
        elapsed_time,
        corrected_elapsed_time,
        age,
    )


class MusicAssistantSource(BaseMetadataSource):
    """
    Music Assistant integration.
    
    Provides real-time metadata and playback controls from any Music Assistant
    server (standalone or Home Assistant add-on).
    
    Configuration:
    - system.music_assistant.server_url: MA server URL (e.g., http://192.168.1.100:8095)
    - system.music_assistant.token: API token (generate in MA web UI)
    - system.music_assistant.player_id: Specific player to monitor (optional)
    - system.music_assistant.paused_timeout: Seconds before paused state expires
    """
    
    def __init__(self, target_player_id: Optional[str] = None):
        super().__init__()
        self._last_active_time = 0
        self._target_player_id = (target_player_id or "").strip() or None

    def _resolve_player_id(self) -> Optional[str]:
        """Pick the MA player id for control commands (volume, devices, etc.).

        Mirrors :meth:`_resolve_queue_id` but for player-scoped operations.
        """
        if self._target_player_id:
            return self._target_player_id
        return _get_target_player_id()

    async def _resolve_queue_id(self) -> Optional[str]:
        """Pick the MA queue id for control commands.

        When the source was constructed with an explicit ``target_player_id``
        (multi-player mode: the request is scoped to a specific player), we
        always resolve the queue from that player so commands operate on the
        UI-selected device.

        Otherwise we use the cached values from the most recent get_metadata()
        call.  If those are empty (cold start / no player scope in URL), we
        fall back to auto-detecting the active player via _resolve_player_id()
        so controls work without an explicit ?player= parameter.
        """
        if self._target_player_id:
            if not _client:
                return None
            return await _get_active_queue_id(self._target_player_id)

        if _current_queue_id:
            return _current_queue_id

        # Cache miss — wait for MA to finish start_listening() before trying
        # to auto-detect, otherwise _client.players.players may be empty.
        if not is_ready():
            await _wait_for_ready(timeout=3.0)

        player_id = self._resolve_player_id()
        logger.debug(
            "MA _resolve_queue_id: auto-detect player_id=%r current_queue=%r current_player=%r ready=%s",
            player_id, _current_queue_id, _current_player_id, is_ready(),
        )
        if player_id and _client:
            return await _get_active_queue_id(player_id)

        logger.warning(
            "MA _resolve_queue_id: no queue found — target=%r current_queue=%r "
            "current_player=%r ready=%s players_count=%d",
            self._target_player_id, _current_queue_id, _current_player_id,
            is_ready(), len(list(_client.players.players)) if _client else -1,
        )
        return _current_player_id or None
    
    @classmethod
    def get_config(cls) -> SourceConfig:
        return SourceConfig(
            name="music_assistant",
            display_name="Music Assistant",
            platforms=["Windows", "Linux", "Darwin"],  # Cross-platform
            default_enabled=True,  # Enabled by default (requires server_url to work)
            default_priority=1,    # High priority (same as Windows Media)
            paused_timeout=600,    # 10 minutes
            requires_auth=True,    # Needs server_url and optional token
            config_keys=[
                "system.music_assistant.server_url",
                "system.music_assistant.token",
                "system.music_assistant.player_id",
            ],
        )
    
    @classmethod
    def capabilities(cls) -> SourceCapability:
        return (
            SourceCapability.METADATA |
            SourceCapability.PLAYBACK_CONTROL |
            SourceCapability.SEEK |
            SourceCapability.DURATION |
            SourceCapability.ALBUM_ART |
            SourceCapability.QUEUE |
            SourceCapability.FAVORITES
        )
    
    def is_available(self) -> bool:
        """
        Check if Music Assistant is available.
        
        Returns True if:
        - Server URL is configured
        - Platform is supported (all platforms)
        """
        return is_configured()
    
    def _map_ma_repeat_mode(self, repeat_mode) -> str:
        """Map MA's RepeatMode enum to our string format ('off', 'context', 'track')."""
        if repeat_mode is None:
            return 'off'
        mode_value = repeat_mode.value if hasattr(repeat_mode, 'value') else str(repeat_mode)
        # MA uses: OFF, ONE, ALL -> we use: 'off', 'track', 'context'
        mode_map = {'off': 'off', 'one': 'track', 'all': 'context'}
        return mode_map.get(mode_value.lower(), 'off')
    
    async def get_metadata(self) -> Optional[Dict[str, Any]]:
        """
        Fetch metadata from Music Assistant.
        
        Gets current track info from the active player's queue.
        Uses cached data if fresh enough, otherwise fetches from server.
        """
        global _metadata_cache, _cache_time, _current_player_id, _last_active_time
        
        # Ensure connected
        if not await _ensure_connected_nonblocking():
            logger.info("MA get_metadata: returning None because not connected")
            return None
        
        try:
            # 1. Get target player ID
            target_id = self._resolve_player_id()
            if not target_id:
                logger.info("MA get_metadata: returning None because no target_id resolved")
                # Rate limit this log to avoid spam
                global _last_no_player_log
                now = time.time()
                if now - _last_no_player_log >= LOG_THROTTLE_INTERVAL:
                    _last_no_player_log = now
                return None
            
            target_player = _client.players.get(target_id) if _client else None

            # 2. Get active queue for this target.
            # This is robust: MA automatically routes child players to their active sync group queue.
            queue_id = await _get_active_queue_id(target_id)
            if not queue_id:
                logger.debug(f"MA get_metadata: no queue found for target {target_id}")
                _log_selected_player_state_change(
                    target_id=target_id,
                    target_player=target_player,
                    queue_id=None,
                    queue=None,
                    coordinator_player=None,
                    is_playing=False,
                    reason="no_active_queue",
                )
                return {"is_playing": False, "source": "music_assistant"}
            
            queue = _client.player_queues.get(queue_id)
            if not queue:
                logger.debug(f"MA get_metadata: queue {queue_id} not found in client")
                _log_selected_player_state_change(
                    target_id=target_id,
                    target_player=target_player,
                    queue_id=queue_id,
                    queue=None,
                    coordinator_player=None,
                    is_playing=False,
                    reason="queue_not_cached",
                )
                return {"is_playing": False, "source": "music_assistant"}
                
            # 3. Get the actual active player object handling playback (e.g., the Sync Group leader).
            # In MA, queue_id is identical to the player_id of the active coordinator.
            player = _client.players.get(queue_id)
            if not player:
                # Fallback to the original target_id if queue_id doesn't map to a player
                player = _client.players.get(target_id)
                
            if not player:
                _log_selected_player_state_change(
                    target_id=target_id,
                    target_player=target_player,
                    queue_id=queue_id,
                    queue=queue,
                    coordinator_player=None,
                    is_playing=None,
                    reason="coordinator_player_not_found",
                )
                logger.info(f"MA get_metadata: returning None because player for queue {queue_id} not found")
                return None
            
            _current_player_id = player.player_id
            
            # Check queue state (use queue.state for consistency with corrected_elapsed_time)
            queue_state = _state_str(queue.state)
            player_state = _state_str(player.playback_state)

            # Determine staleness: how long since MA last updated the position
            # This distinguishes "just paused" from "stale session data from hours ago"
            time_since_update = time.time() - queue.elapsed_time_last_updated
            paused_timeout = self.get_config().paused_timeout  # Default 600s (10 min)
            is_stale = time_since_update > paused_timeout
            
            # Treat as playing if EITHER the resolved player OR its queue
            # reports "playing". The two fields disagree in real setups:
            #   - Sync groups: the leader's queue.state is "playing" while a
            #     child player's playback_state can be "idle"/"paused".
            #   - Transient flap: player_state oscillates playing→idle→playing
            #     between polls during steady playback (see commits 74985f3,
            #     3f693a8); trusting only player_state lets a single bad poll
            #     hand the frontend a hard `is_playing: false`, which sticks
            #     the transport icon on "play" and freezes lyrics until the
            #     user toggles playback manually.
            # Using a union means a "playing" reading from either field wins,
            # which matches what the MA UI itself shows.
            is_playing = player_state == "playing" or queue_state == "playing"

            # Stale-idle means MA's cached queue state is too old to trust
            # (e.g. an external/radio source MA is no longer actively tracking,
            # or a session left idle for >paused_timeout). In that situation MA
            # genuinely does NOT know whether audio is currently playing, so do
            # not hand the frontend a hard `is_playing: false` — that freezes
            # the transport icon on "play" and stops lyric animation even though
            # the recognition engine is decoding live, advancing audio.
            #
            # Return `is_playing: None` (unknown) instead. The frontend's
            # position-advance heuristic then decides from the live recognition
            # position: a real radio/external stream keeps advancing → playing,
            # while a truly paused stream stays put → paused.
            if queue_state == "idle" and is_stale and not is_playing:
                _log_selected_player_state_change(
                    target_id=target_id,
                    target_player=target_player,
                    queue_id=queue_id,
                    queue=queue,
                    coordinator_player=player,
                    is_playing=None,
                    reason="stale_idle",
                )
                return {"is_playing": None, "source": "music_assistant"}

            if is_playing:
                reason = "playing_signal_stale_queue" if queue_state == "idle" and is_stale else "playing_signal"
            else:
                reason = "not_playing_signal"

            _log_selected_player_state_change(
                target_id=target_id,
                target_player=target_player,
                queue_id=queue_id,
                queue=queue,
                coordinator_player=player,
                is_playing=is_playing,
                reason=reason,
            )

            # The MA queue's current_item only reflects what is actually
            # playing when the queue itself is the active source
            # (queue_state == "playing"). When an external source drives the
            # group — e.g. Spotify Connect or AirPlay — the queue stays idle and
            # its current_item is a stale leftover from the last MA-native track
            # (observed: queue frozen on an ELO track while Spotify Connect
            # played something else). MA tracks the *live* external source on
            # the player object instead — player.current_media for identity and
            # player.corrected_elapsed_time for position — which is exactly what
            # the MA UI media card shows. Use that so the song AND the timecode
            # match MA precisely (and freeze correctly on pause). Only when the
            # player exposes no live media do we hand back transport state alone
            # and let recognition own identity/lyrics/position.
            # Decide whether an EXTERNAL source (Spotify Connect / AirPlay) is
            # driving the player. If so the queue's current_item is a stale
            # leftover and we must read identity/position from the live player
            # object; otherwise the queue is authoritative.
            #
            # MA tells us directly: player.active_source is the queue/player id
            # for MA-native queue playback, and the plugin source id for an
            # external source (this is what MA's "External source active" label
            # uses). That's definitive — no timing guess. Only when active_source
            # is unset do we fall back to a staleness heuristic: the queue is
            # treated as live while it is playing or was updated in the last 5s
            # (covers the brief MA-native skip transient where queue_state flips
            # to idle but already holds the new item at elapsed ~0, while the
            # player object's elapsed still lags on the previous track).
            active_source = getattr(player, "active_source", None)
            if active_source:
                external_source = active_source not in (
                    queue_id, getattr(player, "player_id", None)
                )
            else:
                external_source = (
                    queue_state != "playing"
                    and (time.time() - queue.elapsed_time_last_updated) >= 5.0
                )
            global _last_source_select_log
            _sel = (active_source, queue_id, external_source)
            if _sel != _last_source_select_log:
                _last_source_select_log = _sel
                logger.info(
                    "MA source select: active_source=%r queue_id=%r queue_state=%s "
                    "external=%s", active_source, queue_id, queue_state, external_source,
                )
            if external_source:
                pm = getattr(player, "current_media", None)
                pm_title = getattr(pm, "title", None) if pm else None
                pm_artist = getattr(pm, "artist", None) if pm else None
                if pm and pm_title and pm_artist:
                    pm_pos = getattr(player, "corrected_elapsed_time", None)
                    if pm_pos is None:
                        pm_pos = getattr(player, "elapsed_time", None)
                    pm_duration = getattr(pm, "duration", None)
                    pm_media_type = getattr(pm, "media_type", None)
                    result = {
                        "is_playing": is_playing,
                        "source": "music_assistant",
                        "artist": pm_artist,
                        "artist_name": pm_artist,
                        "title": pm_title,
                        "album": getattr(pm, "album", None),
                        "position": pm_pos if pm_pos is not None else 0,
                        "duration_ms": int(pm_duration * 1000) if pm_duration else None,
                        "media_type": getattr(pm_media_type, "value", None)
                        or (str(pm_media_type) if pm_media_type else "track"),
                    }
                    pm_art = getattr(pm, "image_url", None)
                    if pm_art:
                        result["album_art_url"] = pm_art
                    return result
                return {"is_playing": is_playing, "source": "music_assistant"}

            # Get current item from queue
            current_item = queue.current_item
            if not current_item:
                return {"is_playing": is_playing, "source": "music_assistant"}
            
            # Extract metadata
            media_item = current_item.media_item
            if not media_item:
                # Use queue item directly if no media_item
                artist = current_item.name or ""
                title = ""
                album = None
            else:
                # Get from media_item (more detailed)
                artist = ""
                if hasattr(media_item, 'artists') and media_item.artists:
                    artist = media_item.artists[0].name if media_item.artists else ""
                elif hasattr(media_item, 'artist'):
                    artist = str(media_item.artist) if media_item.artist else ""
                
                title = media_item.name or ""
                album = media_item.album.name if hasattr(media_item, 'album') and media_item.album else None
            
            # Handle case where title is empty but name exists on current_item
            if not title and current_item.name:
                title = current_item.name
            
            # Get image URL
            album_art_url = None
            try:
                # Try to get image from the client's helper
                album_art_url = _client.get_media_item_image_url(current_item, size=640)
            except Exception:
                pass
            
            # Calculate position
            # IMPORTANT: Only use corrected_elapsed_time when PLAYING
            # When paused/stopped, use raw elapsed_time to avoid infinite interpolation
            # (corrected_elapsed_time interpolates based on queue.state, which can get stuck)
            #
            # TODO: MA Server PR #2959 (Jan 2026) adds elapsed_time_updated_at from server
            # This will eliminate clock drift between MA server and client by providing
            # the server's timestamp when elapsed_time was measured.
            # Track: https://github.com/music-assistant/server/pull/2959
            # When merged, update to use server timestamp instead of client time.time()
            # Current workaround: users adjust music_assistant_latency_compensation setting
            #
            if is_playing:
                position = queue.corrected_elapsed_time if queue.corrected_elapsed_time is not None else 0
            else:
                # Paused - use raw elapsed_time (no interpolation)
                position = queue.elapsed_time if queue.elapsed_time is not None else 0
            
            # Get duration
            duration_ms = None
            if current_item.duration:
                duration_ms = int(current_item.duration * 1000)
            
            # Update last active time
            if is_playing:
                self._last_active_time = time.time()
            
            # Get MA item ID for favorites support
            # Use media_item.item_id if available, fallback to uri
            ma_item_id = None
            if media_item:
                if hasattr(media_item, 'item_id') and media_item.item_id:
                    ma_item_id = str(media_item.item_id)
                elif hasattr(media_item, 'uri') and media_item.uri:
                    ma_item_id = str(media_item.uri)
            
            # Get media type to identify radio streams
            media_type = "track"
            if media_item and hasattr(media_item, 'media_type'):
                # Handle enum if present, otherwise string
                media_type = media_item.media_type.value if hasattr(media_item.media_type, 'value') else str(media_item.media_type)
            elif current_item and hasattr(current_item, 'media_type'):
                media_type = current_item.media_type.value if hasattr(current_item.media_type, 'value') else str(current_item.media_type)
            
            # Build result
            result = {
                "track_id": _normalize_track_id(artist, title),
                "artist": artist,
                "artist_name": artist,  # For display consistency with other sources
                "title": title,
                "album": album,
                "album_art_url": album_art_url,
                "position": position,
                "duration_ms": duration_ms,
                "is_playing": is_playing,
                "media_type": media_type,
                "source": "music_assistant",
                "colors": ("#24273a", "#363b54"),  # Default, will be enriched
                "last_active_time": self._last_active_time,
                "ma_item_id": ma_item_id,  # For favorites/like functionality
                # Shuffle/repeat state for UI buttons (fetched from queue)
                "shuffle_state": queue.shuffle_enabled if hasattr(queue, 'shuffle_enabled') else False,
                "repeat_state": self._map_ma_repeat_mode(queue.repeat_mode) if hasattr(queue, 'repeat_mode') else 'off',
            }
            
            return result
            
        except Exception as e:
            logger.info(f"MA get_metadata: returning None due to exception: {e}", exc_info=True)
            # Rate limit metadata error log
            global _last_metadata_error_log
            now = time.time()
            if now - _last_metadata_error_log >= LOG_THROTTLE_INTERVAL:
                _last_metadata_error_log = now
            # Don't set _connected = False here - that causes reconnect spam
            # Connection errors are handled by start_listening task
            return None
    
    # === Playback Controls ===
    
    async def _send_player_command(self, command: str) -> bool:
        """Send a player-level transport command to the resolved coordinator.

        Transport (play/pause/stop/skip) must target the *player* — and the
        sync-group coordinator when synced — not the queue. Sending a queue
        command to a group whose active source is external (e.g. Spotify
        Connect) makes MA resume the group's own MA queue from playlog instead
        of controlling the live source, which is what caused playback to jump
        to a previously played MA-direct track. MA's own transport bar uses
        these ``players/cmd/*`` commands, which route to the active source.
        """
        if not await _ensure_connected():
            return False
        try:
            player_id = self._resolve_player_id()
            if not player_id:
                logger.warning(
                    "MA %s: no player resolved for target=%r",
                    command, self._target_player_id,
                )
                return False
            player_id = _resolve_group_target_id(player_id)
            logger.info("MA %s: player_id=%r", command, player_id)
            await _client.send_command(f"players/cmd/{command}", player_id=player_id)
            return True
        except Exception as e:
            logger.warning(
                "MA %s exception for target=%r: %s",
                command, self._target_player_id, e,
            )
            return False

    async def toggle_playback(self) -> bool:
        """Toggle play/pause on the resolved player/coordinator."""
        return await self._send_player_command("play_pause")

    async def play(self) -> bool:
        """Resume playback."""
        return await self._send_player_command("play")

    async def pause(self) -> bool:
        """Pause playback."""
        return await self._send_player_command("pause")

    async def next_track(self) -> bool:
        """Skip to next track."""
        return await self._send_player_command("next")

    async def previous_track(self) -> bool:
        """Skip to previous track."""
        return await self._send_player_command("previous")
    
    async def seek(self, position_ms: int) -> bool:
        """Seek to position in milliseconds."""
        if not await _ensure_connected():
            return False
        
        try:
            queue_id = await self._resolve_queue_id()
            if not queue_id:
                return False
            
            # MA seek expects seconds
            position_seconds = position_ms // 1000
            await _client.player_queues.seek(queue_id, position_seconds)
            return True
        except Exception as e:
            logger.debug(f"Music Assistant seek failed: {e}")
            return False
    
    async def get_queue(self) -> Optional[Dict]:
        """
        Get playback queue (upcoming songs only).
        
        Returns queue in Spotify-compatible format for frontend compatibility.
        Only returns songs AFTER the current playing song, not history.
        """
        if not await _ensure_connected():
            return None
        
        try:
            queue_id = await self._resolve_queue_id()
            if not queue_id:
                return None
            
            # Resolve current position to know where to start the upcoming queue.
            # Prefer the in-memory cached queue (populated by start_listening events).
            # If not cached yet, fall back to a direct get_active_queue() API call so
            # the queue works immediately after connection (before full event sync).
            queue_obj = _client.player_queues.get(queue_id)
            if not queue_obj:
                logger.debug("Music Assistant get_queue: queue_id=%r not in client cache; querying API", queue_id)
                try:
                    queue_obj = await _client.player_queues.get_active_queue(queue_id)
                except Exception:
                    queue_obj = None

            current_index = getattr(queue_obj, 'current_index', None)
            if current_index is None:
                current_index = 0

            # Get items starting AFTER the current item.
            # offset = current_index + 1 skips history AND the currently playing song.
            items = await _client.player_queues.get_queue_items(
                queue_id,
                limit=20,
                offset=current_index + 1
            )
            
            # Convert to Spotify-compatible format
            queue_items = []
            for i, item in enumerate(items):
                media = item.media_item
                if not media:
                    continue

                # Get artist name
                artist_name = ""
                if hasattr(media, 'artists') and media.artists:
                    artist_name = media.artists[0].name
                elif hasattr(media, 'artist'):
                    artist_name = str(media.artist) if media.artist else ""

                # Get album art
                art_url = None
                try:
                    art_url = _client.get_media_item_image_url(item, size=64)
                except Exception:
                    pass

                queue_items.append({
                    "name": media.name or item.name,
                    "artists": [{"name": artist_name}],
                    "album": {
                        "images": [{"url": art_url}] if art_url else []
                    },
                    # Absolute index in the full queue (for play-from-here)
                    "queue_index": current_index + 1 + i,
                    # Unique item ID — more reliable than positional index in
                    # recent MA server versions where play_index accepts either.
                    "queue_item_id": getattr(item, 'queue_item_id', None),
                })
            
            return {
                "queue": queue_items,
                "source": "music_assistant"
            }
            
        except Exception as e:
            logger.debug(f"Music Assistant get_queue failed: {e}")
            return None

    async def play_queue_item(self, queue_index: int, queue_item_id: Optional[str] = None) -> bool:
        """Jump to a specific item in the queue and start playing.

        Prefers ``queue_item_id`` (a stable string UUID) over the integer
        positional ``queue_index`` because recent MA server versions accept
        either form and the ID is immune to mid-playback queue reordering.
        """
        if not await _ensure_connected():
            return False
        try:
            queue_id = await self._resolve_queue_id()
            if not queue_id:
                logger.warning(
                    "MA play_queue_item: could not resolve queue_id for target=%r",
                    self._target_player_id,
                )
                return False
            # queue_item_id (string) is preferred; integer index is the fallback.
            item_ref = queue_item_id if queue_item_id else queue_index
            try:
                await _client.player_queues.play_index(queue_id, item_ref)
            except AttributeError:
                # Older client builds: fall back to raw WebSocket command.
                cmd_kwargs: dict = {"queue_id": queue_id}
                if queue_item_id:
                    cmd_kwargs["queue_item_id"] = queue_item_id
                else:
                    cmd_kwargs["index"] = queue_index
                await _client.send_command("player_queues/play_index", **cmd_kwargs)
            logger.info(
                "MA play_queue_item: queue_id=%r index=%d item_id=%r",
                queue_id, queue_index, queue_item_id,
            )
            return True
        except Exception as e:
            logger.warning(
                "MA play_queue_item failed: queue_id=%r index=%d item_id=%r error=%s",
                None, queue_index, queue_item_id, e,
            )
            return False

    # === Favorites (Like) Support ===
    
    async def is_favorite(self, item_id: str) -> bool:
        """
        Check if the current track is in favorites.
        
        Uses a cache to avoid spamming the API. Cache is invalidated
        when add_to_favorites or remove_from_favorites is called.
        
        Args:
            item_id: MA media item ID
            
        Returns:
            True if track is in favorites, False otherwise
        """
        global _favorite_cache, _favorite_cache_time
        
        if not item_id:
            return False
        
        # Check cache first
        now = time.time()
        if item_id in _favorite_cache:
            cache_age = now - _favorite_cache_time.get(item_id, 0)
            if cache_age < FAVORITE_CACHE_TTL:
                return _favorite_cache[item_id]
        
        if not await _ensure_connected_nonblocking():
            return False
        
        try:
            # Get the current queue to check the playing item's favorite status
            queue_id = await self._resolve_queue_id()
            if not queue_id:
                return False
            
            queue = _client.player_queues.get(queue_id)
            if not queue or not queue.current_item:
                return False
            
            # Check if the current item's media_item has favorite property
            media_item = queue.current_item.media_item
            is_fav = False
            if media_item and hasattr(media_item, 'favorite'):
                is_fav = media_item.favorite
            
            # Cache the result
            _favorite_cache[item_id] = is_fav
            _favorite_cache_time[item_id] = now
            
            return is_fav
            
        except Exception as e:
            logger.debug(f"Music Assistant is_favorite check failed: {e}")
            return False
    
    async def add_to_favorites(self, item_id: str) -> bool:
        """
        Add a track to favorites.
        
        Args:
            item_id: MA media item ID
            
        Returns:
            True if successful, False otherwise
        """
        if not item_id:
            logger.debug(f"Music Assistant add_to_favorites: item_id is empty/None")
            return False
        
        if not await _ensure_connected_nonblocking():
            return False
        
        try:
            # Get the current queue to access the media_item object
            # The add_item command needs the actual media item, not just the ID
            queue_id = await self._resolve_queue_id()
            if queue_id:
                queue = _client.player_queues.get(queue_id)
                if queue and queue.current_item and queue.current_item.media_item:
                    media_item = queue.current_item.media_item
                    # Use the media item directly
                    await _client.send_command(
                        "music/favorites/add_item",
                        item=media_item
                    )
                    # Invalidate cache - set to True so next check returns correct state
                    _favorite_cache[item_id] = True
                    _favorite_cache_time[item_id] = time.time()
                    logger.debug(f"Added track {item_id} to MA favorites")
                    return True
            
            logger.debug(f"Music Assistant add_to_favorites: no current media item available")
            return False
            
        except Exception as e:
            logger.debug(f"Music Assistant add_to_favorites failed: {e}")
            return False
    
    async def remove_from_favorites(self, item_id: str) -> bool:
        """
        Remove a track from favorites.
        
        Args:
            item_id: MA media item ID
            
        Returns:
            True if successful, False otherwise
        """
        if not item_id:
            logger.debug(f"Music Assistant remove_from_favorites: item_id is empty/None")
            return False
        
        if not await _ensure_connected_nonblocking():
            return False
        
        try:
            # remove_item uses library_item_id parameter
            await _client.send_command(
                "music/favorites/remove_item",
                library_item_id=item_id,
                media_type="track"
            )
            # Invalidate cache - set to False so next check returns correct state
            _favorite_cache[item_id] = False
            _favorite_cache_time[item_id] = time.time()
            logger.debug(f"Removed track {item_id} from MA favorites")
            return True
            
        except Exception as e:
            logger.debug(f"Music Assistant remove_from_favorites failed: {e}")
            return False
    
    # === Volume Control ===
    
    async def get_volume(self) -> Optional[int]:
        """Get current player volume (0-100).
        
        Returns:
            Volume percentage (0-100) or None if unavailable
        """
        if not await _ensure_connected_nonblocking():
            return None
        
        try:
            player_id = self._resolve_player_id()
            if not player_id:
                return None
            
            player = _client.players.get(player_id)
            if player and hasattr(player, 'volume_level'):
                # MA volume is 0-100
                return int(player.volume_level)
            return None
        except Exception as e:
            logger.debug(f"Music Assistant get_volume failed: {e}")
            return None
    
    async def set_volume(self, volume: int) -> bool:
        """Set player volume (0-100).
        
        Args:
            volume: Volume percentage (0-100)
            
        Returns:
            True if successful, False otherwise
        """
        if not await _ensure_connected_nonblocking():
            return False
        
        # Clamp to valid range
        volume = max(0, min(100, volume))
        
        try:
            player_id = self._resolve_player_id()
            if not player_id:
                return False
            
            await _client.players.volume_set(player_id, volume)
            logger.debug(f"Set MA player volume to {volume}%")
            return True
        except Exception as e:
            logger.debug(f"Music Assistant set_volume failed: {e}")
            return False

    # === Shuffle Control ===
    
    async def get_shuffle(self) -> Optional[bool]:
        """Get current shuffle state.
        
        Returns:
            True if shuffle enabled, False if disabled, None if unavailable
        """
        if not await _ensure_connected_nonblocking():
            return None
        
        try:
            player_id = self._resolve_player_id()
            if not player_id:
                return None
            
            queue_id = await _get_active_queue_id(player_id)
            if not queue_id:
                return None
            
            queue = _client.player_queues.get(queue_id)
            if queue and hasattr(queue, 'shuffle_enabled'):
                return bool(queue.shuffle_enabled)
            return None
        except Exception as e:
            logger.debug(f"Music Assistant get_shuffle failed: {e}")
            return None
    
    async def set_shuffle(self, enabled: bool) -> bool:
        """Set shuffle mode.
        
        Args:
            enabled: True to enable shuffle, False to disable
            
        Returns:
            True if successful, False otherwise
        """
        if not await _ensure_connected_nonblocking():
            return False
        
        try:
            player_id = self._resolve_player_id()
            if not player_id:
                return False
            
            queue_id = await _get_active_queue_id(player_id)
            if not queue_id:
                return False
            
            await _client.player_queues.shuffle(queue_id, enabled)
            logger.info(f"Set MA shuffle to {enabled}")
            return True
        except Exception as e:
            logger.debug(f"Music Assistant set_shuffle failed: {e}")
            return False
    
    # === Repeat Control ===
    
    async def get_repeat(self) -> Optional[str]:
        """Get current repeat mode.
        
        Returns:
            'off', 'all', or 'one' (mapped from MA's RepeatMode enum), None if unavailable
        """
        if not await _ensure_connected_nonblocking():
            return None
        
        try:
            player_id = self._resolve_player_id()
            if not player_id:
                return None
            
            queue_id = await _get_active_queue_id(player_id)
            if not queue_id:
                return None
            
            queue = _client.player_queues.get(queue_id)
            if queue and hasattr(queue, 'repeat_mode'):
                # Map MA's RepeatMode to our string format
                # MA uses: OFF, ONE, ALL
                # We use: 'off', 'track' (for ONE), 'context' (for ALL)
                mode_value = queue.repeat_mode.value if hasattr(queue.repeat_mode, 'value') else str(queue.repeat_mode)
                mode_map = {'off': 'off', 'one': 'track', 'all': 'context'}
                return mode_map.get(mode_value.lower(), 'off')
            return None
        except Exception as e:
            logger.debug(f"Music Assistant get_repeat failed: {e}")
            return None
    
    async def set_repeat(self, mode: str) -> bool:
        """Set repeat mode.
        
        Args:
            mode: 'off', 'context' (all), or 'track' (one)
            
        Returns:
            True if successful, False otherwise
        """
        if not await _ensure_connected_nonblocking():
            return False
        
        try:
            from music_assistant_models.enums import RepeatMode
            
            # Map our mode names to MA's RepeatMode
            mode_map = {'off': RepeatMode.OFF, 'context': RepeatMode.ALL, 'track': RepeatMode.ONE}
            ma_mode = mode_map.get(mode)
            if not ma_mode:
                logger.warning(f"Invalid repeat mode: {mode}")
                return False
            
            player_id = self._resolve_player_id()
            if not player_id:
                return False
            
            queue_id = await _get_active_queue_id(player_id)
            if not queue_id:
                return False
            
            await _client.player_queues.repeat(queue_id, ma_mode)
            logger.info(f"Set MA repeat to {mode}")
            return True
        except ImportError:
            logger.debug("music_assistant_models not available for repeat mode")
            return False
        except Exception as e:
            logger.debug(f"Music Assistant set_repeat failed: {e}")
            return False
    
    # === Device/Player Selection ===
    
    async def get_devices(self) -> List[Dict[str, Any]]:
        """Get list of available players/devices.
        
        Returns:
            List of player dicts with id, name, type, is_active, state
        """
        if not await _ensure_connected_nonblocking():
            return []
        
        try:
            devices = []
            current_player_id = self._resolve_player_id()
            
            for player in _client.players:
                devices.append({
                    'id': player.player_id,
                    'name': player.name or player.player_id,
                    'type': player.type.value if hasattr(player.type, 'value') else str(player.type),
                    'is_active': player.player_id == current_player_id,
                    'state': player.playback_state.value if player.playback_state else 'idle',
                    'volume': int(player.volume_level) if hasattr(player, 'volume_level') and player.volume_level is not None else None,
                })
            
            return devices
        except Exception as e:
            logger.debug(f"Music Assistant get_devices failed: {e}")
            return []
    
    async def transfer_playback(self, target_player_id: str) -> bool:
        """Transfer playback to another player.
        
        Args:
            target_player_id: ID of the player to transfer to
            
        Returns:
            True if successful, False otherwise
        """
        if not await _ensure_connected_nonblocking():
            return False
        
        try:
            source_player_id = self._resolve_player_id()
            if not source_player_id:
                return False
            
            source_queue_id = await _get_active_queue_id(source_player_id)
            if not source_queue_id:
                return False
            
            # For transfer, target queue ID is usually same as player ID
            target_queue_id = target_player_id
            
            await _client.player_queues.transfer(source_queue_id, target_queue_id)
            logger.info(f"Transferred MA playback from {source_player_id} to {target_player_id}")
            return True
        except Exception as e:
            logger.debug(f"Music Assistant transfer_playback failed: {e}")
            return False


# =============================================================================
# Lifecycle Management (Non-blocking connection startup/shutdown)
# =============================================================================

def start_background_connection():
    """
    Start background connection task at app startup.
    
    Call this when MA is configured to begin connection in background.
    Non-blocking - connection happens asynchronously.
    """
    global _connection_task, _connecting
    
    if not is_configured():
        return
    
    if _connecting or (_connection_task and not _connection_task.done()):
        return  # Already connecting
    
    _connecting = True
    _connection_task = asyncio.create_task(_background_connect())
    logger.debug("Started Music Assistant background connection task")


def stop_background_connection():
    """
    Cancel background connection task and disconnect client on shutdown.
    
    Call this on app exit for clean shutdown.
    """
    global _connection_task, _connecting, _listener_task, _client, _connected, _listening
    
    _connecting = False
    
    # Cancel background connection task
    if _connection_task and not _connection_task.done():
        _connection_task.cancel()
        _connection_task = None
        logger.debug("Cancelled Music Assistant background connection task")
    
    # Cancel listener task
    if _listener_task and not _listener_task.done():
        _listener_task.cancel()
        _listener_task = None
        logger.debug("Cancelled Music Assistant listener task")
    
    # Disconnect client (sync wrapper - schedules async disconnect)
    if _client:
        try:
            # Try to get running loop and schedule disconnect
            loop = asyncio.get_running_loop()
            loop.create_task(_disconnect_client())
        except RuntimeError:
            # No running loop - we're in sync context during shutdown
            # Client will be garbage collected, which triggers cleanup
            pass
        _client = None
        _connected = False
        _listening = False
        logger.debug("Music Assistant client marked for disconnect")


async def _disconnect_client():
    """Helper to disconnect client asynchronously."""
    global _client
    if _client:
        try:
            await _client.disconnect()
            logger.debug("Music Assistant client disconnected")
        except Exception as e:
            logger.debug(f"Error disconnecting MA client: {e}")

