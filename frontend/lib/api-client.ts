/**
 * Typed API client for the Cilex Vision Query API.
 *
 * Base URL is read from NEXT_PUBLIC_API_URL (default http://localhost:8000).
 * All requests include credentials (cookies) for JWT auth.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

export interface BBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface DetectionResponse {
  time: string;
  camera_id: string;
  frame_seq: number;
  object_class: string;
  confidence: number;
  bbox: BBox;
  local_track_id: string | null;
  model_version: string;
}

export interface DetectionListResponse {
  items: DetectionResponse[];
  total: number;
  offset: number;
  limit: number;
}

export interface TrackSummaryResponse {
  local_track_id: string;
  camera_id: string;
  object_class: string;
  state: string;
  mean_confidence: number | null;
  start_time: string;
  end_time: string | null;
  tracker_version: string | null;
  created_at: string;
}

export interface TrackAttributeResponse {
  attribute_id: string;
  attribute_type: string;
  color_value: string;
  confidence: number;
  model_version: string | null;
  observed_at: string;
}

export interface TrackDetailResponse extends TrackSummaryResponse {
  attributes: TrackAttributeResponse[];
  thumbnail_url: string | null;
}

export interface TrackListResponse {
  items: TrackSummaryResponse[];
  total: number;
  offset: number;
  limit: number;
}

export interface EventResponse {
  event_id: string;
  event_type: string;
  track_id: string | null;
  camera_id: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  clip_url: string | null;
  state: string;
  metadata: Record<string, unknown> | null;
  source_capture_ts: string | null;
  edge_receive_ts: string | null;
  core_ingest_ts: string | null;
}

export interface EventListResponse {
  items: EventResponse[];
  total: number;
  offset: number;
  limit: number;
}

export interface TopologyNode {
  camera_id: string;
  name: string;
  zone_id: string | null;
}

export interface TopologyEdge {
  camera_a_id: string;
  camera_b_id: string;
  transition_time_s: number;
}

export interface TopologyResponse {
  site_id: string;
  cameras: TopologyNode[];
  edges: TopologyEdge[];
}

// ---------------------------------------------------------------------------
// Query parameter types
// ---------------------------------------------------------------------------

export interface DetectionQuery {
  camera_id?: string;
  start?: string;
  end?: string;
  object_class?: string;
  min_confidence?: number;
  offset?: number;
  limit?: number;
}

export interface TrackQuery {
  camera_id?: string;
  start?: string;
  end?: string;
  object_class?: string;
  state?: string;
  offset?: number;
  limit?: number;
}

export interface EventQuery {
  site_id?: string;
  camera_id?: string;
  start?: string;
  end?: string;
  event_type?: string;
  state?: string;
  offset?: number;
  limit?: number;
}

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

function buildUrl(path: string, params?: Record<string, string | number | undefined>): string {
  const url = new URL(path, API_BASE);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") {
        url.searchParams.set(k, String(v));
      }
    }
  }
  return url.toString();
}

async function apiFetch<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = buildUrl(path, params);
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API methods
// ---------------------------------------------------------------------------

export async function getDetections(q: DetectionQuery = {}): Promise<DetectionListResponse> {
  return apiFetch<DetectionListResponse>("/detections", q as Record<string, string | number | undefined>);
}

export async function getTracks(q: TrackQuery = {}): Promise<TrackListResponse> {
  return apiFetch<TrackListResponse>("/tracks", q as Record<string, string | number | undefined>);
}

export async function getTrackDetail(localTrackId: string): Promise<TrackDetailResponse> {
  return apiFetch<TrackDetailResponse>(`/tracks/${localTrackId}`);
}

export async function getEvents(q: EventQuery = {}): Promise<EventListResponse> {
  return apiFetch<EventListResponse>("/events", q as Record<string, string | number | undefined>);
}

export async function getTopology(siteId: string): Promise<TopologyResponse> {
  return apiFetch<TopologyResponse>(`/topology/${siteId}`);
}
