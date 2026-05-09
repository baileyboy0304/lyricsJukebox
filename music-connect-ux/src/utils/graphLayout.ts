import type { ArtistNode } from '../types';
import { clamp } from './normalise';

export const targetPosition = (index: number, total: number, similarity: number, cx: number, cy: number) => {
  const angle = (index / total) * Math.PI * 2;
  const r = clamp(300 - similarity * 150, 140, 300);
  return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
};

export const pickOffscreen = (w: number, h: number) => {
  const side = Math.floor(Math.random() * 4);
  if (side === 0) return { x: -120, y: Math.random() * h };
  if (side === 1) return { x: w + 120, y: Math.random() * h };
  if (side === 2) return { x: Math.random() * w, y: -120 };
  return { x: Math.random() * w, y: h + 120 };
};

export const hasDataForArtist = (node: ArtistNode, keys: string[]) => keys.includes(node.id);
