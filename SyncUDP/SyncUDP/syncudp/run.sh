#!/bin/bash
# SyncLyrics (UDP Only) - Home Assistant Addon Entrypoint
# Reads options from /data/options.json and maps them to environment variables.

set -e

OPTIONS_FILE="/data/options.json"

if [ ! -f "$OPTIONS_FILE" ]; then
    echo "ERROR: $OPTIONS_FILE not found - is this running as an HA addon?"
    exit 1
fi

echo "============================================"
echo "  SyncLyrics (UDP Only) - HA Addon Starting"
echo "============================================"

# Read UDP-only options from HA config
MUSIC_ASSISTANT_BASE_URL=$(jq -r '.music_assistant_base_url // .music_assistant_url // empty' "$OPTIONS_FILE")
MUSIC_ASSISTANT_TOKEN=$(jq -r '.music_assistant_token // empty' "$OPTIONS_FILE")
MUSIC_ASSISTANT_PLAYER_ID=$(jq -r '.music_assistant_player_id // empty' "$OPTIONS_FILE")
LASTFM_API_KEY=$(jq -r '.lastfm_api_key // empty' "$OPTIONS_FILE")
SERVER_PORT=$(jq -r '.server_port // 9012' "$OPTIONS_FILE")
HTTPS_ENABLED=$(jq -r '.https_enabled // true' "$OPTIONS_FILE")
HTTPS_PORT=$(jq -r '.https_port // 9013' "$OPTIONS_FILE")
RECOGNITION_ENABLED=$(jq -r '.recognition_enabled // true' "$OPTIONS_FILE")
UDP_AUDIO_PORT=$(jq -r '.udp_listen_port // .udp_audio_port // 6056' "$OPTIONS_FILE")
UDP_AUDIO_SAMPLE_RATE=$(jq -r '.udp_audio_sample_rate // 16000' "$OPTIONS_FILE")
UDP_JITTER_BUFFER_MS=$(jq -r '.udp_jitter_buffer_ms // 60' "$OPTIONS_FILE")
PLAYERS_AUTO_DISCOVER=$(jq -r '.players_auto_discover // true' "$OPTIONS_FILE")
PLAYERS_JSON=$(jq -c '.players // []' "$OPTIONS_FILE")
DEBUG_LOG_LEVEL="INFO"

# Export environment variables for the application
export SERVER_PORT
export DEBUG_LOG_LEVEL
export UDP_AUDIO_ENABLED=true
export UDP_AUDIO_PORT
export UDP_AUDIO_SAMPLE_RATE
export UDP_JITTER_BUFFER_MS
export AUDIO_RECOGNITION_ENABLED="$RECOGNITION_ENABLED"
export PLAYERS_AUTO_DISCOVER
export PLAYERS_JSON

# Optional variables (only export if set)
[ -n "$LASTFM_API_KEY" ] && export LASTFM_API_KEY
[ -n "$MUSIC_ASSISTANT_BASE_URL" ] && export SYSTEM_MUSIC_ASSISTANT_SERVER_URL="$MUSIC_ASSISTANT_BASE_URL"
[ -n "$MUSIC_ASSISTANT_TOKEN" ] && export SYSTEM_MUSIC_ASSISTANT_TOKEN="$MUSIC_ASSISTANT_TOKEN"
[ -n "$MUSIC_ASSISTANT_PLAYER_ID" ] && export SYSTEM_MUSIC_ASSISTANT_PLAYER_ID="$MUSIC_ASSISTANT_PLAYER_ID"

# HTTPS config
export SERVER_HTTPS_ENABLED="$HTTPS_ENABLED"
export SERVER_HTTPS_PORT="$HTTPS_PORT"

# Persistent storage paths (use /config for addon_config mount)
export SYNCLYRICS_SETTINGS_FILE="/config/settings.json"
export SYNCLYRICS_STATE_FILE="/config/state.json"
export SYNCLYRICS_LYRICS_DB="/config/lyrics_database"
export SYNCLYRICS_ALBUM_ART_DB="/config/album_art_database"
export SYNCLYRICS_CACHE_DIR="/config/cache"
export SYNCLYRICS_LOGS_DIR="/config/logs"
export SYNCLYRICS_CERTS_DIR="/config/certs"
export DESKTOP="Linux"
export PYTHONUNBUFFERED=1

# Create persistent storage directories
mkdir -p "$SYNCLYRICS_LYRICS_DB" \
         "$SYNCLYRICS_ALBUM_ART_DB" \
         "$SYNCLYRICS_CACHE_DIR" \
         "$SYNCLYRICS_LOGS_DIR" \
         "$SYNCLYRICS_CERTS_DIR"

# Log configuration
echo ""
echo "Configuration:"
echo "  Server Port: $SERVER_PORT"
echo "  HTTPS: $HTTPS_ENABLED (port $HTTPS_PORT)"
echo "  Log Level: $DEBUG_LOG_LEVEL"
echo ""
echo "UDP Audio:"
echo "  Enabled: true"
echo "  Port: $UDP_AUDIO_PORT"
echo "  Jitter Buffer: ${UDP_JITTER_BUFFER_MS} ms"
echo "  Sample Rate: ${UDP_AUDIO_SAMPLE_RATE} Hz"
echo ""
echo "Data: /config"
echo ""
[ -n "$LASTFM_API_KEY" ] && echo "  Last.fm: configured"
[ -n "$MUSIC_ASSISTANT_BASE_URL" ] && echo "  Music Assistant: $MUSIC_ASSISTANT_BASE_URL"
echo "============================================"
echo ""

# Run SyncLyrics
exec python3 sync_lyrics.py
