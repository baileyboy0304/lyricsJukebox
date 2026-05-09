import { neighbourhoods } from '../mockData';
import type { ArtistNeighbourhood } from '../types';

export const lookupNeighbourhood = (term: string): ArtistNeighbourhood | undefined => {
  const normal = term.trim().toLowerCase();
  return Object.values(neighbourhoods).find((n) => n.artist.id === normal || n.artist.name.toLowerCase() === normal);
};
