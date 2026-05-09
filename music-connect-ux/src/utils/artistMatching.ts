const STOP_WORDS = new Set(['the', 'and']);

export const normaliseName = (value: string) =>
  value
    .toLowerCase()
    .replace(/\([^)]*\)/g, ' ')
    .replace(/\[[^\]]*\]/g, ' ')
    .replace(/&/g, ' and ')
    .replace(/\bft\.?\b|\bfeat\.?\b|\bfeaturing\b/gi, ' ')
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

const fingerprint = (value: string) =>
  normaliseName(value)
    .split(' ')
    .filter((t) => t && !STOP_WORDS.has(t))
    .join(' ');

export const artistMatches = (candidate: string, target: string): boolean => {
  if (!candidate || !target) return false;
  const a = normaliseName(candidate);
  const b = normaliseName(target);
  if (a === b) return true;
  const fa = fingerprint(candidate);
  const fb = fingerprint(target);
  if (fa && fa === fb) return true;
  if (fa && fb && (fa.includes(fb) || fb.includes(fa))) return true;
  return false;
};

export const extractArtists = (item: any): string[] => {
  const cands = [item?.artists, item?.artist, item?.album?.artists, item?.current_media?.artists, item?.current_item?.artists, item?.current_media_item?.artists];
  const out: string[] = [];
  for (const c of cands) {
    if (Array.isArray(c)) for (const v of c) if (typeof v === 'string') out.push(v); else if (v?.name) out.push(v.name);
    else if (typeof c === 'string') out.push(c);
  }
  return out.filter(Boolean);
};

export const artistListIncludes = (artists: string[], activeArtist: string) =>
  artists.some((a) => artistMatches(a, activeArtist));
