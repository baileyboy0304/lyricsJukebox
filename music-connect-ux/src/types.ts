export type ArtistNode = {
  id: string;
  name: string;
  image?: string;
  similarity?: number;
};
export type MockAlbum = { id: string; title: string; artistId: string; artistName: string; year?: number; popularity?: number; artwork?: string };
export type MockTrack = { id: string; title: string; artistId: string; artistName: string; album?: string; popularity?: number; artwork?: string };
export type MockPlayer = { id: string; name: string; status: 'playing' | 'paused' | 'idle'; currentTrack: string; currentArtist: string; artwork?: string };
export type ArtistNeighbourhood = { artist: ArtistNode; similarArtists: ArtistNode[]; albums: MockAlbum[]; tracks: MockTrack[] };
export type GraphPhase = 'idle' | 'imploding' | 'pop-in' | 'inflate-selected' | 'explode-out' | 'recenter-new-active' | 'fly-in-new-neighbours' | 'collision-pulse' | 'settle';
