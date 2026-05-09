import { useEffect, useRef, useState } from 'react';
import type { ArtistNode, MediaItem, PlayerItem } from '../api/apiTypes';
import { loadConfig } from '../api/configClient';
import { getSimilarArtists, getTopAlbums } from '../api/lastfmClient';
import { getAlbumTracks, getPlayers, playMedia, searchMusic } from '../api/musicAssistantClient';
import type { GraphPhase } from '../types';
import { artistListIncludes, artistMatches, extractArtists, normaliseName } from '../utils/artistMatching';
import { pickArray, unwrapResult } from '../utils/maResponse';
import { BubbleGraph } from './BubbleGraph';
import { ManualArtistSearch } from './ManualArtistSearch';
import { MediaPanel } from './MediaPanel';
import { PlayerBar } from './PlayerBar';


const toPrimaryArtist = (value: string) => {
  const clean = value.trim();
  if (!clean) return value;
  // Keep duo/band names such as "Mumford & Sons" intact; only strip explicit featuring/collab separators.
  const lead = clean.split(/\s+(?:feat\.?|ft\.?)\s+/i)[0]?.trim() || clean;
  if (lead.includes('/')) return lead.split('/').map((v) => v.trim()).find(Boolean) ?? lead;
  return lead;
};

export function MusicNeighbourhoodPage() {
  const [activeArtist, setActiveArtist] = useState<ArtistNode>({ id: 'initial', name: 'Loading...' });
  const [similarArtists, setSimilarArtists] = useState<ArtistNode[]>([]);
  const [albums, setAlbums] = useState<MediaItem[]>([]);
  const [tracks, setTracks] = useState<MediaItem[]>([]);
  const [players, setPlayers] = useState<PlayerItem[]>([]);
  const [selectedPlayer, setSelectedPlayer] = useState('');
  const [phase, setPhase] = useState<GraphPhase>('idle');
  const [showLines, setShowLines] = useState<boolean>(() => {
    const v = typeof window !== 'undefined' ? localStorage.getItem('music-connect:show-lines') : null;
    return v === null ? true : v === '1';
  });
  const [error, setError] = useState('');
  const [loadingMedia, setLoadingMedia] = useState(false);
  const [albumTrackMap, setAlbumTrackMap] = useState<Record<string, MediaItem[]>>({});
  const didBootstrap = useRef(false);
  const lastTrackSig = useRef('');
  const lastArtistKey = useRef(''); // currently-loaded active artist
  const lastPlayerArtistKey = useRef(''); // artist last reported by the player; only updates from polling/bootstrap
  const similarCountRef = useRef(0);
  const artistLoadSeq = useRef(0);

  useEffect(() => { similarCountRef.current = similarArtists.length; }, [similarArtists]);

  const loadArtist = async (artistName: string) => {
    const primaryArtist = toPrimaryArtist(artistName);
    if (!primaryArtist.trim()) { setError('Enter an artist name.'); return; }
    const key = normaliseName(primaryArtist);
    // Same artist already loaded with bubbles? Don't reload — would clear the network briefly.
    if (key === lastArtistKey.current && similarCountRef.current > 0) {
      return;
    }
    lastArtistKey.current = key;
    const requestSeq = ++artistLoadSeq.current;
    console.log('Active artist change', artistName, '->', primaryArtist);
    setLoadingMedia(true);
    setError('');
    setSimilarArtists([]);
    setAlbumTrackMap({});
    setActiveArtist({ id: key.replace(/\s+/g, '-'), name: primaryArtist });
    try {
      const search = await searchMusic(primaryArtist, ['artist', 'album', 'track'], 80);
      const searchRoot = unwrapResult(search);
      console.log('[MA] search response keys', Object.keys(search || {}));
      const artistsRaw = searchRoot?.artists ?? searchRoot?.artist ?? [];
      const artistCandidates = Array.isArray(artistsRaw) ? artistsRaw : [];
      const exact = artistCandidates.find((a: any) => artistMatches(a?.name || '', primaryArtist));
      const mbid = exact?.mbid || exact?.metadata?.mbid || '';
      console.log('[MA] artist match', { requested: primaryArtist, matched: exact?.name, mbid: Boolean(mbid) });
      const [similar, topAlbums] = await Promise.all([getSimilarArtists(primaryArtist, 20, mbid), getTopAlbums(primaryArtist, 30, mbid)]);
      const similarsRaw = Array.isArray(similar?.similarartists?.artist) ? similar.similarartists.artist : similar?.similarartists?.artist ? [similar.similarartists.artist] : [];
      const similarAttrArtist = similar?.similarartists?.['@attr']?.artist ?? '';
      const similarAttrNorm = normaliseName(toPrimaryArtist(similarAttrArtist));
      const requestedNorm = normaliseName(primaryArtist);
      const similarMatchesRequested = !similarAttrArtist || similarAttrNorm === requestedNorm;
      if (!similarMatchesRequested) {
        console.warn('[Last.fm] ignoring mismatched similar-artist payload', { requested: primaryArtist, returned: similarAttrArtist });
      }
      if (requestSeq !== artistLoadSeq.current) { console.log('[MA] stale similar response ignored', primaryArtist, requestSeq); return; }
      const similarList = similarMatchesRequested ? similarsRaw : [];
      setSimilarArtists(similarList.slice(0, 20).map((a: any) => ({ id: normaliseName(a.name).replace(/\s+/g, '-'), name: a.name, similarity: Number(a.match) || 0.2 })));
      const albumsRaw = searchRoot?.album ?? searchRoot?.albums ?? searchRoot?.results?.album ?? [];
      const tracksRaw = searchRoot?.track ?? searchRoot?.tracks ?? searchRoot?.results?.track ?? [];
      console.log('[MA] search parsed counts', { albums: Array.isArray(albumsRaw) ? albumsRaw.length : 0, tracks: Array.isArray(tracksRaw) ? tracksRaw.length : 0 });
      const allAlbums = (Array.isArray(albumsRaw) ? albumsRaw : []).filter((a: any) => artistListIncludes(extractArtists(a), primaryArtist));
      const topList = Array.isArray(topAlbums?.topalbums?.album) ? topAlbums.topalbums.album : [];
      const rank = new Map(topList.map((a: any, i: number) => [normaliseName(a.name), i]));
      const albumItems = allAlbums.map((a: any) => ({ id: a.item_id || a.uri || a.name, uri: a.uri || '', title: a.name || 'Unknown', artistName: extractArtists(a)[0] || artistName, year: a.year, artwork: a.image, provider: a.provider_mappings?.[0]?.provider_instance || a.provider || '', raw: a })) as MediaItem[];
      albumItems.sort((a, b) => Number(rank.get(normaliseName(a.title)) ?? 9999) - Number(rank.get(normaliseName(b.title)) ?? 9999));
      if (requestSeq !== artistLoadSeq.current) return;
      setAlbums(albumItems.slice(0, 30));

      const allTracks = (Array.isArray(tracksRaw) ? tracksRaw : []).filter((t: any) => artistListIncludes(extractArtists(t), primaryArtist));
      const dedup = new Map<string, MediaItem>();
      for (const t of allTracks) {
        const item: MediaItem = { id: t.item_id || t.uri || t.name, uri: t.uri || '', title: t.name || 'Unknown', artistName: extractArtists(t)[0] || primaryArtist, album: t.album?.name, popularity: t.metadata?.popularity ?? 0, artwork: t.image, raw: t };
        const key = normaliseName(item.title);
        const existing = dedup.get(key);
        if (!existing || (item.popularity ?? 0) > (existing.popularity ?? 0)) dedup.set(key, item);
      }
      if (requestSeq !== artistLoadSeq.current) return;
      setTracks(Array.from(dedup.values()).sort((a, b) => (b.popularity ?? 0) - (a.popularity ?? 0)).slice(0, 30));
      if ((similarMatchesRequested ? similarsRaw : []).length === 0) setError('Last.fm no similar artists');
    } catch (e: any) {
      if (requestSeq === artistLoadSeq.current) setError(e.message || 'Failed loading artist');
    } finally { if (requestSeq === artistLoadSeq.current) setLoadingMedia(false); }
  };

  useEffect(() => {
    if (didBootstrap.current) return;
    didBootstrap.current = true;
    (async () => {
    try {
      const config = await loadConfig();
      if (!config.has_music_assistant_url) throw new Error('missing Music Assistant URL');
      if (!config.has_music_assistant_token) throw new Error('missing Music Assistant token');
      if (!config.has_lastfm_api_key) throw new Error('missing Last.fm API key');
      const playersData = await getPlayers();
      const playersRoot = unwrapResult(playersData);
      const list = pickArray(playersRoot);
      console.log('[MA] players response keys', Object.keys(playersData || {}));
      console.log('[MA] players parsed count', list.length);
      const pickTitle = (p: any) => p.current_media?.name || p.current_item?.name || p.current_media_item?.name || p.current_media?.title || p.current_item?.title || p.current_media_item?.title || p.media_title || 'N/A';
      const pickArtist = (p: any) => p.current_media?.artist || p.current_item?.artist || p.current_media_item?.artist || p.media_artist || 'N/A';
      const mapped = list.map((p: any) => ({ id: p.player_id || p.id, name: p.display_name || p.name || 'Unknown', status: p.state || p.status || 'unknown', currentTrack: pickTitle(p), currentArtist: pickArtist(p), raw: p }));
      setPlayers(mapped);
      const remembered = localStorage.getItem('music-connect:selected-player') ?? '';
      const selected = mapped.find((p: PlayerItem) => p.id === remembered)?.id || mapped.find((p: PlayerItem) => p.id === config.default_player)?.id || mapped[0]?.id || '';
      setSelectedPlayer(selected);
      localStorage.setItem('music-connect:selected-player', selected);
      const seedArtist = mapped.find((p: PlayerItem) => p.id === selected)?.currentArtist;
      const seedTrack = mapped.find((p: PlayerItem) => p.id === selected)?.currentTrack || 'N/A';
      if (seedArtist && seedArtist !== 'N/A') {
        lastTrackSig.current = `${seedTrack}::${seedArtist}`;
        lastPlayerArtistKey.current = normaliseName(toPrimaryArtist(seedArtist));
        loadArtist(seedArtist);
      } else { setActiveArtist({ id: 'manual', name: 'Enter artist' }); setError('No artist currently playing. Enter an artist manually.'); }
    } catch (e: any) { setError(e.message); }
  })(); }, []);


  useEffect(() => {
    if (!selectedPlayer) return;
    const timer = setInterval(async () => {
      try {
        const playersData = await getPlayers();
        const list = pickArray(unwrapResult(playersData));
        const p = list.find((x: any) => (x.player_id || x.id) === selectedPlayer);
        if (!p) return;
        const title = p.current_media?.name || p.current_item?.name || p.current_media_item?.name || p.current_media?.title || p.media_title || 'N/A';
        const artist = p.current_media?.artist || p.current_item?.artist || p.current_media_item?.artist || p.media_artist || 'N/A';
        setPlayers((prev) => prev.map((pl) => pl.id === selectedPlayer ? { ...pl, status: p.state || p.status || pl.status, currentTrack: title, currentArtist: artist } : pl));
        lastTrackSig.current = `${title}::${artist}`;
        if (artist && artist !== 'N/A') {
          const newKey = normaliseName(toPrimaryArtist(artist));
          // Only follow the player when it actually moves to a new artist on its own,
          // never to override a user's manual navigation.
          if (newKey && newKey !== lastPlayerArtistKey.current) {
            console.log('[MA] player advanced to new artist', artist);
            lastPlayerArtistKey.current = newKey;
            loadArtist(toPrimaryArtist(artist));
          }
        }
      } catch (e) {
        console.error('[MA] polling failed', e);
      }
    }, 2500);
    return () => clearInterval(timer);
  }, [selectedPlayer]);

  return (
    <div className="page">
      <main>
        <div className="toolbar">
          <h1>Music Connect</h1>
          <ManualArtistSearch onSearch={(t) => loadArtist(t)} />
          <label className="toggle">
            <input
              type="checkbox"
              checked={showLines}
              onChange={(e) => {
                setShowLines(e.target.checked);
                localStorage.setItem('music-connect:show-lines', e.target.checked ? '1' : '0');
              }}
            />
            <span>Show connections</span>
          </label>
          {error && <p className="error">{error}</p>}
        </div>
        <BubbleGraph activeArtist={activeArtist} similarArtists={similarArtists} onSelectArtist={(a) => { loadArtist(a.name); return true; }} phase={phase} setPhase={(p) => { setPhase(p); }} showLines={showLines} />
      </main>
      <MediaPanel
        albums={albums}
        tracks={tracks}
        albumTrackMap={albumTrackMap}
        onExpandAlbum={async (album) => {
          if (albumTrackMap[album.id]) return;
          try {
            const rawTracks = await getAlbumTracks({ uri: album.uri, itemId: album.id, provider: album.provider, raw: album.raw });
            const items = rawTracks
              .slice()
              .sort((a: any, b: any) => {
                const ad = (a.disc_number ?? 1) - (b.disc_number ?? 1);
                if (ad !== 0) return ad;
                const at = (a.track_number ?? 999) - (b.track_number ?? 999);
                if (at !== 0) return at;
                return String(a.name ?? '').localeCompare(String(b.name ?? ''));
              })
              .map((t: any) => ({
                id: t.uri || t.item_id || `${album.id}::${t.disc_number ?? 1}::${t.track_number ?? 0}::${t.name ?? ''}`,
                uri: t.uri || '',
                title: t.name || t.title || 'Unknown',
                artistName: extractArtists(t)[0] || album.artistName,
                album: album.title,
                popularity: t.metadata?.popularity ?? 0,
                duration: t.duration,
                raw: t
              } as MediaItem));
            setAlbumTrackMap((prev) => ({ ...prev, [album.id]: items }));
          } catch (e) {
            console.error('[MA] album tracks failed', album.id, e);
            setAlbumTrackMap((prev) => ({ ...prev, [album.id]: [] }));
            setError('Could not load album tracks');
          }
        }}
        loading={loadingMedia}
        onExploreArtist={(artist) => artist && artist !== activeArtist.name && loadArtist(artist)}
        onPlayAlbum={async (a) => {
          if (!selectedPlayer) return setError('no selected player');
          if (!a.uri) return setError('playback request failed');
          try { await playMedia(selectedPlayer, a.uri); } catch { setError('playback request failed'); }
        }}
        onPlayTrack={async (t) => {
          if (!selectedPlayer) return setError('no selected player');
          if (!t.uri) return setError('playback request failed');
          try { await playMedia(selectedPlayer, t.uri); } catch { setError('playback request failed'); }
        }}
      />
      <PlayerBar players={players} selected={selectedPlayer} onSelect={(id) => { console.log('Selected player', id); setSelectedPlayer(id); localStorage.setItem('music-connect:selected-player', id); }} onSeed={() => { const p = players.find((x) => x.id === selectedPlayer); if (!p?.currentArtist || p.currentArtist === 'N/A') { setError('No artist currently playing on this player.'); return; } const seedPrimary = toPrimaryArtist(p.currentArtist); lastPlayerArtistKey.current = normaliseName(seedPrimary); loadArtist(seedPrimary); }} />
    </div>
  );
}
