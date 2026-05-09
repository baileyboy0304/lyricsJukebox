import type { AppConfig } from './apiTypes';

export async function loadConfig(): Promise<AppConfig> {
  const r = await fetch('/local-config');
  const data = await r.json();
  if (!r.ok || !data.ok) {
    throw new Error(data.error ?? 'Failed to load config');
  }
  return data.config;
}
