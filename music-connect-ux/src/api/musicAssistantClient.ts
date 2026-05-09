const parseResponse = async (r: Response) => {
  const text = await r.text();
  try { return JSON.parse(text); } catch { return { error: text || 'Unknown error' }; }
};

export async function postMusicAssistantCommand(command: string, args: Record<string, unknown>) {
  console.log('[MA] command', command, Object.keys(args));
  const r = await fetch('/dev-api/music-assistant', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command, args }) });
  const data = await parseResponse(r);
  if (!r.ok) { console.error('[MA] command failed', command, data); throw new Error(data.error ?? data.message ?? `Music Assistant command failed (${command})`); }
  return data;
}

export const getPlayers = () => postMusicAssistantCommand('players/all', {});
export const searchMusic = (query: string, mediaTypes: string[], limit: number) => postMusicAssistantCommand('music/search', { search_query: query, media_types: mediaTypes, limit });

const URI_RE = /^([^:]+):\/\/album\/(.+)$/;

const parseAlbumUri = (uri?: string): { provider: string; itemId: string } | null => {
  if (!uri) return null;
  const m = URI_RE.exec(uri);
  if (!m) return null;
  return { provider: m[1], itemId: m[2] };
};

type ProviderMapping = { item_id?: string; provider_instance?: string; provider_domain?: string };

const collectAlbumLookups = (album: { uri?: string; itemId?: string; provider?: string; raw?: any }): Array<{ itemId: string; provider: string }> => {
  const seen = new Set<string>();
  const out: Array<{ itemId: string; provider: string }> = [];
  const push = (itemId?: string, provider?: string) => {
    if (!itemId || !provider) return;
    const key = `${provider}::${itemId}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ itemId, provider });
  };
  const parsed = parseAlbumUri(album.uri);
  if (parsed) push(parsed.itemId, parsed.provider);
  const mappings: ProviderMapping[] = Array.isArray(album.raw?.provider_mappings) ? album.raw.provider_mappings : [];
  for (const pm of mappings) {
    push(pm.item_id, pm.provider_instance || pm.provider_domain);
    if (pm.provider_domain && pm.provider_instance && pm.provider_domain !== pm.provider_instance) {
      push(pm.item_id, pm.provider_domain);
    }
  }
  if (album.itemId && album.provider) push(album.itemId, album.provider);
  return out;
};

export type AlbumLookup = { uri?: string; itemId?: string; provider?: string; raw?: any };

export async function getAlbumTracks(album: AlbumLookup): Promise<any[]> {
  const lookups = collectAlbumLookups(album);
  let lastError: Error | null = null;
  for (const { itemId, provider } of lookups) {
    try {
      const data = await postMusicAssistantCommand('music/albums/album_tracks', {
        item_id: itemId,
        provider_instance_id_or_domain: provider,
        in_library_only: false
      });
      const tracks = extractTracksFromAlbumResponse(data);
      if (tracks.length > 0) return tracks;
    } catch (e) {
      lastError = e as Error;
      console.warn('[MA] album_tracks failed', { itemId, provider, message: lastError.message });
    }
  }
  if (album.uri) {
    try {
      const item = await postMusicAssistantCommand('music/item_by_uri', { uri: album.uri });
      const result = item?.result ?? item;
      const itemId = result?.item_id;
      const provider = result?.provider || result?.provider_instance;
      if (itemId && provider) {
        const data = await postMusicAssistantCommand('music/albums/album_tracks', {
          item_id: itemId,
          provider_instance_id_or_domain: provider,
          in_library_only: false
        });
        return extractTracksFromAlbumResponse(data);
      }
    } catch (e) {
      lastError = e as Error;
      console.warn('[MA] item_by_uri fallback failed', album.uri, lastError.message);
    }
  }
  if (lastError) throw lastError;
  return [];
}

const extractTracksFromAlbumResponse = (data: any): any[] => {
  const root = data?.result ?? data;
  if (Array.isArray(root)) return root;
  if (Array.isArray(root?.tracks)) return root.tracks;
  if (Array.isArray(root?.items)) return root.items;
  return [];
};

export async function playMedia(playerId: string, mediaUri: string) {
  return postMusicAssistantCommand('player_queues/play_media', { queue_id: playerId, media: [mediaUri] });
}
