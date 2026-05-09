import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

type SecretsConfig = {
  music_assistant_url?: string;
  music_assistant_token?: string;
  lastfm_api_key?: string;
  default_player?: string;
  max_similar_artists?: number;
  max_albums?: number;
  max_tracks?: number;
};

const buildMusicAssistantCandidates = (raw: string): string[] => {
  const trimmed = raw.trim();
  const withProtocol = /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;
  const input = new URL(withProtocol);
  const hadPort = Boolean(input.port);
  const ports = hadPort ? [input.port] : ['8095', input.protocol === 'https:' ? '443' : '80'];
  const basePath = '/api';

  const candidates: string[] = [];
  for (const port of ports) {
    const u = new URL(input.toString());
    u.port = port;
    u.pathname = basePath;
    u.search = '';
    u.hash = '';
    candidates.push(u.toString().replace(/\/$/, ''));
  }
  return Array.from(new Set(candidates));
};

const safeParseSecrets = (secretsPath: string): { ok: true; config: SecretsConfig } | { ok: false; message: string } => {
  if (!fs.existsSync(secretsPath)) return { ok: false, message: 'Missing secrets.json. Copy secrets.example.json to secrets.json and enter your Music Assistant and Last.fm values.' };
  try {
    return { ok: true, config: JSON.parse(fs.readFileSync(secretsPath, 'utf-8')) as SecretsConfig };
  } catch {
    return { ok: false, message: 'Invalid secrets.json. Check JSON syntax.' };
  }
};

const readJsonBody = async (req: any) => {
  const chunks: Uint8Array[] = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString('utf-8').trim();
  if (!raw) return {};
  return JSON.parse(raw) as Record<string, unknown>;
};


const postJson = (targetUrl: string, token: string, payload: Record<string, unknown>) => new Promise<{ status: number; body: string }>((resolve, reject) => {
  const url = new URL(targetUrl);
  const isHttps = url.protocol === 'https:';
  const lib = isHttps ? https : http;
  const req = lib.request({
    protocol: url.protocol,
    hostname: url.hostname,
    port: url.port || (isHttps ? 443 : 80),
    path: `${url.pathname}${url.search}`,
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    ...(isHttps ? { rejectUnauthorized: false } : {})
  }, (res) => {
    const chunks: Uint8Array[] = [];
    res.on('data', (c) => chunks.push(c));
    res.on('end', () => resolve({ status: res.statusCode ?? 500, body: Buffer.concat(chunks).toString('utf-8') }));
  });
  req.setTimeout(8000, () => req.destroy(new Error(`timeout connecting to ${url.hostname}:${url.port || (isHttps ? 443 : 80)}`)));
  req.on('error', reject);
  req.write(JSON.stringify(payload));
  req.end();
});
export default defineConfig({
  plugins: [
    react(),
    {
      name: 'local-config-and-proxy',
      configureServer(server) {
        server.middlewares.use('/local-config', (_req, res) => {
          const parsed = safeParseSecrets(path.resolve(server.config.root, 'secrets.json'));
          res.setHeader('Content-Type', 'application/json');
          if (!parsed.ok) return void res.end(JSON.stringify({ ok: false, error: parsed.message }));
          const config = parsed.config;
          res.end(JSON.stringify({ ok: true, config: { default_player: config.default_player ?? '', max_similar_artists: config.max_similar_artists ?? 20, max_albums: config.max_albums ?? 30, max_tracks: config.max_tracks ?? 30, has_music_assistant_url: Boolean(config.music_assistant_url?.trim()), has_music_assistant_token: Boolean(config.music_assistant_token?.trim()), has_lastfm_api_key: Boolean(config.lastfm_api_key?.trim()) } }));
        });

        server.middlewares.use('/dev-api/music-assistant', async (req, res) => {
          res.setHeader('Content-Type', 'application/json');
          try {
            if (req.method !== 'POST') { res.statusCode = 405; res.end(JSON.stringify({ error: 'Method not allowed' })); return; }
            const parsed = safeParseSecrets(path.resolve(server.config.root, 'secrets.json'));
            if (!parsed.ok) { res.statusCode = 500; res.end(JSON.stringify({ error: parsed.message })); return; }
            const { music_assistant_url, music_assistant_token } = parsed.config;
            if (!music_assistant_url?.trim()) { res.statusCode = 400; res.end(JSON.stringify({ error: 'missing Music Assistant URL' })); return; }
            if (!music_assistant_token?.trim()) { res.statusCode = 400; res.end(JSON.stringify({ error: 'missing Music Assistant token' })); return; }

            const body = await readJsonBody(req);
            const command = typeof body.command === 'string' ? body.command : '';
            const args = (body.args && typeof body.args === 'object') ? (body.args as Record<string, unknown>) : {};
            if (!command) { res.statusCode = 400; res.end(JSON.stringify({ error: 'missing command' })); return; }

            const payload = { message_id: `${Date.now()}-${Math.random().toString(16).slice(2)}`, command, args };
            const candidates = buildMusicAssistantCandidates(music_assistant_url);
            let lastError: Error | null = null;
            let result: { status: number; body: string } | null = null;

            for (const endpoint of candidates) {
              try {
                result = await postJson(endpoint, music_assistant_token, payload);
                if (result.status >= 200 && result.status < 300) break;
                console.error('[MA] upstream error', result.status, command, endpoint);
              } catch (error) {
                lastError = error as Error;
                console.error('[MA] proxy candidate failure', endpoint, lastError.message);
              }
            }

            if (!result) {
              res.statusCode = 502;
              res.end(JSON.stringify({ error: `Music Assistant unavailable. Tried: ${candidates.join(', ')}. ${lastError?.message ?? ''}` }));
              return;
            }

            if (result.status < 200 || result.status >= 300) {
              res.statusCode = result.status;
              res.end(result.body || JSON.stringify({ error: 'Music Assistant upstream error' }));
              return;
            }

            res.statusCode = result.status;
            res.end(result.body);
          } catch (error) {
            console.error('[MA] proxy failure', (error as Error).message);
            res.statusCode = 500;
            res.end(JSON.stringify({ error: 'Music Assistant unavailable' }));
          }
        });

        const lastFmProxy = (method: string) => async (req: any, res: any) => {
          res.setHeader('Content-Type', 'application/json');
          try {
            const parsed = safeParseSecrets(path.resolve(server.config.root, 'secrets.json'));
            if (!parsed.ok) { res.statusCode = 500; res.end(JSON.stringify({ error: parsed.message })); return; }
            const { lastfm_api_key } = parsed.config;
            if (!lastfm_api_key?.trim()) { res.statusCode = 400; res.end(JSON.stringify({ error: 'missing Last.fm API key' })); return; }
            const url = new URL(req.url ?? '', 'http://localhost');
            const artist = url.searchParams.get('artist') ?? '';
            const mbid = url.searchParams.get('mbid') ?? '';
            const limit = url.searchParams.get('limit') ?? (method === 'artist.getSimilar' ? '20' : '30');
            const autocorrect = url.searchParams.get('autocorrect') ?? '0';
            if (!artist.trim() && !mbid.trim()) { res.statusCode = 400; res.end(JSON.stringify({ error: 'missing artist/mbid' })); return; }
            const target = `https://ws.audioscrobbler.com/2.0/?method=${encodeURIComponent(method)}&${mbid ? `mbid=${encodeURIComponent(mbid)}` : `artist=${encodeURIComponent(artist)}`}&api_key=${encodeURIComponent(lastfm_api_key)}&format=json&limit=${encodeURIComponent(limit)}&autocorrect=${encodeURIComponent(autocorrect)}`;
            const r = await fetch(target);
            res.statusCode = r.status;
            res.end(await r.text());
          } catch (error) {
            console.error('[Last.fm] proxy failure', (error as Error).message);
            res.statusCode = 500;
            res.end(JSON.stringify({ error: 'Last.fm unavailable' }));
          }
        };

        server.middlewares.use('/dev-api/lastfm/similar', lastFmProxy('artist.getSimilar'));
        server.middlewares.use('/dev-api/lastfm/top-albums', lastFmProxy('artist.getTopAlbums'));
      }
    }
  ]
});
