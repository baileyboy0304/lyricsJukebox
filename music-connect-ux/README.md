# music-connect-ux

Standalone React/Vite UX prototype for Music Assistant + Last.fm artist neighbourhood exploration.

## Setup

1. Install dependencies:
   - `npm install`
2. Copy local config:
   - `cp secrets.example.json secrets.json`
3. Edit `secrets.json` with your local values.
4. Run dev server:
   - `npm run dev`

If `secrets.json` is missing, the UI shows:

`Missing secrets.json. Copy secrets.example.json to secrets.json and enter your Music Assistant and Last.fm values.`

## Notes

- This app remains a standalone browser UX app (not a Home Assistant custom integration).
- Dev middleware serves:
  - `GET /local-config`
  - `POST /dev-api/music-assistant`
  - `GET /dev-api/lastfm/similar`
  - `GET /dev-api/lastfm/top-albums`
- Tokens are kept in `secrets.json` server-side and not sent in `/local-config`.

This standalone UX prototype loads API credentials locally for development. For a deployed Home Assistant version, API calls should be proxied server-side so Music Assistant and Last.fm credentials are not exposed to the browser.
