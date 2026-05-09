export type AppConfig = {
  default_player: string;
  max_similar_artists: number;
  max_albums: number;
  max_tracks: number;
  has_music_assistant_url: boolean;
  has_music_assistant_token: boolean;
  has_lastfm_api_key: boolean;
};

export type ArtistNode = { id: string; name: string; image?: string; similarity?: number };
export type MediaItem = { id: string; uri: string; title: string; artistName: string; album?: string; year?: number; popularity?: number; artwork?: string; duration?: number; provider?: string; raw?: Record<string, unknown> };
export type PlayerItem = { id: string; name: string; status: string; currentTrack: string; currentArtist: string; artwork?: string; raw?: Record<string, unknown> };
