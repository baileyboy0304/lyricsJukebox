import { useState } from 'react';

export function ManualArtistSearch({ onSearch }: { onSearch: (term: string) => void }) {
  const [value, setValue] = useState('');
  return (
    <form className="search" onSubmit={(e) => { e.preventDefault(); onSearch(value); }}>
      <input value={value} onChange={(e) => setValue(e.target.value)} placeholder="Find artist" />
      <button type="submit">Explore</button>
    </form>
  );
}
