import { useMemo, useRef, useState } from 'react';
import type { MediaItem } from '../api/apiTypes';

type Props = {
  albums: MediaItem[];
  tracks: MediaItem[];
  albumTrackMap: Record<string, MediaItem[]>;
  onExpandAlbum: (a: MediaItem) => void;
  onPlayAlbum: (a: MediaItem) => void;
  onPlayTrack: (t: MediaItem) => void;
  onExploreArtist: (artist: string) => void;
  loading: boolean;
};

const DRAG_THRESHOLD = 6;

// Hook: enables mouse drag-to-scroll inside an overflow-y list, the way native
// touch already does on touch devices. Touch / pen are left to the browser.
function useDragToScroll() {
  const ref = useRef<HTMLDivElement>(null);
  const state = useRef<{ pointerId: number; startY: number; startScroll: number; moved: boolean } | null>(null);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.pointerType !== 'mouse') return;
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    // Don't hijack drag when starting on an interactive control
    if (target.closest('button, a, input, select, textarea')) return;
    state.current = {
      pointerId: e.pointerId,
      startY: e.clientY,
      startScroll: ref.current?.scrollTop ?? 0,
      moved: false
    };
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = state.current;
    if (!s || s.pointerId !== e.pointerId) return;
    const dy = e.clientY - s.startY;
    if (!s.moved && Math.abs(dy) < DRAG_THRESHOLD) return;
    if (!s.moved) {
      s.moved = true;
      ref.current?.classList.add('dragging');
      try { (e.currentTarget as Element).setPointerCapture(e.pointerId); } catch { /* ignore */ }
    }
    if (ref.current) ref.current.scrollTop = s.startScroll - dy;
  };

  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = state.current;
    if (!s || s.pointerId !== e.pointerId) return;
    if (s.moved) {
      try { (e.currentTarget as Element).releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      ref.current?.classList.remove('dragging');
    }
    state.current = null;
  };

  // If a drag actually happened, swallow the click that would otherwise fire on a child card.
  const onClickCapture = (e: React.MouseEvent<HTMLDivElement>) => {
    if (state.current && state.current.moved) {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  return {
    ref,
    handlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp: endDrag,
      onPointerCancel: endDrag,
      onClickCapture
    }
  };
}

export function MediaPanel({ albums, tracks, albumTrackMap, onExpandAlbum, onPlayAlbum, onPlayTrack, onExploreArtist, loading }: Props) {
  const [expandedAlbum, setExpandedAlbum] = useState<string>('');
  const albumsScroll = useDragToScroll();
  const tracksScroll = useDragToScroll();

  const uniqAlbums = useMemo(() => {
    const seen = new Set<string>();
    return albums.filter((a) => {
      const k = `${a.title.toLowerCase()}::${a.year ?? ''}`;
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
  }, [albums]);

  return (
    <div className="mediaPanel">
      <section className="mediaColumn">
        <h3>Albums</h3>
        <div className="mediaList" ref={albumsScroll.ref} {...albumsScroll.handlers}>
          {loading && albums.length === 0 && <div className="empty">Loading albums…</div>}
          {!loading && uniqAlbums.length === 0 && <div className="empty">No albums available</div>}
          {uniqAlbums.map((a) => {
            const isOpen = expandedAlbum === a.id;
            const list = albumTrackMap[a.id] ?? [];
            return (
              <div key={a.id}>
                <div
                  className="card"
                  onClick={() => {
                    const next = isOpen ? '' : a.id;
                    setExpandedAlbum(next);
                    if (next) onExpandAlbum(a);
                  }}
                >
                  <button className="playBtn" onClick={(e) => { e.stopPropagation(); onPlayAlbum(a); }} aria-label="Play album">▶</button>
                  <div className="cardBody">
                    <div className="cardTitle">{a.title}</div>
                    <div
                      className="cardSub"
                      onClick={(e) => { e.stopPropagation(); onExploreArtist(a.artistName); }}
                    >
                      {a.artistName}{a.year ? ` · ${a.year}` : ''}
                    </div>
                  </div>
                </div>
                {isOpen && (
                  <div className="albumTracks">
                    {list.length === 0
                      ? <div className="empty">No track listing available</div>
                      : list.map((t) => (
                          <div key={t.id} className="albumTrack">
                            <span className="albumTrackTitle">{t.title}</span>
                            <button onClick={(e) => { e.stopPropagation(); onPlayTrack(t); }} aria-label="Play track">▶</button>
                          </div>
                        ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>

      <section className="mediaColumn">
        <h3>Tracks</h3>
        <div className="mediaList" ref={tracksScroll.ref} {...tracksScroll.handlers}>
          {loading && tracks.length === 0 && <div className="empty">Loading tracks…</div>}
          {!loading && tracks.length === 0 && <div className="empty">No tracks available</div>}
          {tracks.map((t) => (
            <div key={t.id} className="card" onClick={() => onExploreArtist(t.artistName)}>
              <button className="playBtn" onClick={(e) => { e.stopPropagation(); onPlayTrack(t); }} aria-label="Play track">▶</button>
              <div className="cardBody">
                <div className="cardTitle">{t.title}</div>
                <div className="cardSub">{t.artistName}{t.album ? ` · ${t.album}` : ''}</div>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
