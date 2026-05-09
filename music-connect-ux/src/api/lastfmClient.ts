export async function getSimilarArtists(artistName: string, limit: number, mbid?: string) {
  const params = new URLSearchParams({ limit: String(limit), autocorrect: '0' });
  if (mbid) params.set('mbid', mbid);
  else params.set('artist', artistName.trim());
  const r = await fetch(`/dev-api/lastfm/similar?${params.toString()}`);
  if (!r.ok) throw new Error('Last.fm unavailable');
  return r.json();
}

export async function getTopAlbums(artistName: string, limit: number, mbid?: string) {
  const params = new URLSearchParams({ limit: String(limit), autocorrect: '0' });
  if (mbid) params.set('mbid', mbid);
  else params.set('artist', artistName.trim());
  const r = await fetch(`/dev-api/lastfm/top-albums?${params.toString()}`);
  if (!r.ok) throw new Error('Last.fm unavailable');
  return r.json();
}
