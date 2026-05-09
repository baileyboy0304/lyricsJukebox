import type { PlayerItem } from '../api/apiTypes';

export function PlayerBar({
  players, selected, onSelect, onSeed
}: {
  players: PlayerItem[];
  selected: string;
  onSelect: (id: string) => void;
  onSeed: () => void;
}) {
  const p = players.find((x) => x.id === selected);
  const status = p?.status ?? 'unknown';
  const dotClass = status === 'playing' ? 'statusDot playing' : status === 'paused' ? 'statusDot paused' : 'statusDot';
  const title = p?.currentTrack && p.currentTrack !== 'N/A' ? p.currentTrack : '—';
  const artist = p?.currentArtist && p.currentArtist !== 'N/A' ? p.currentArtist : '—';
  return (
    <div className="playerBar">
      <select value={selected} onChange={(e) => onSelect(e.target.value)}>
        {players.map((pl) => <option key={pl.id} value={pl.id}>{pl.name}</option>)}
      </select>
      <span><span className={dotClass} />{status}</span>
      <div className="nowPlaying">
        <span className="title">{title}</span>
        <span className="sub">{artist}</span>
      </div>
      <button onClick={onSeed}>Seed from current player</button>
    </div>
  );
}
