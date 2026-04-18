/**
 * Typed API client for the Cilex Vision Query API.
 *
 * Base URL is read from NEXT_PUBLIC_API_URL (default http://localhost:8000).
 * All requests include credentials (cookies) for JWT auth.
 */

// Browser requests go through the Next.js rewrite proxy (/api/... -> query-api).
// Server-side requests can reach query-api directly via the Docker network.
const API_BASE =
  typeof window !== "undefined"
    ? "/api"
    : process.env.API_URL || process.env.NEXT_PUBLIC_API_URL || "http://query-api:8000";

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
  thumbnail_url: string | null;
}

export interface DetectionListResponse {
  detections: DetectionResponse[];
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
  tracks: TrackSummaryResponse[];
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
  events: EventResponse[];
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
// Topology admin types (full models from query-api)
// ---------------------------------------------------------------------------

export interface CameraNode {
  camera_id: string;
  site_id: string;
  name: string;
  zone_id: string | null;
  latitude: number | null;
  longitude: number | null;
  status: string;
  location_description: string | null;
}

export interface TransitTimeDistribution {
  object_class: string;
  p50_ms: number;
  p90_ms: number;
  p99_ms: number;
  sample_count: number;
  last_updated: string | null;
}

export interface TransitionEdge {
  edge_id: string | null;
  camera_a_id: string;
  camera_b_id: string;
  transition_time_s: number;
  confidence: number;
  enabled: boolean;
  transit_distributions: TransitTimeDistribution[];
}

export interface TopologyGraph {
  site_id: string;
  site_name?: string;
  cameras: CameraNode[];
  edges: TransitionEdge[];
}

export interface CameraCreateRequest {
  camera_id: string;
  name: string;
  zone_id?: string;
  latitude?: number;
  longitude?: number;
  location_description?: string;
}

export interface EdgeCreateRequest {
  camera_a_id: string;
  camera_b_id: string;
  transition_time_s: number;
  confidence?: number;
  enabled?: boolean;
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
  has_thumbnail?: boolean;
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
  has_clip?: boolean;
  offset?: number;
  limit?: number;
}

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

function buildUrl(path: string, params?: Record<string, string | number | undefined>): string {
  // On the client side API_BASE is "/api" (relative), so we can't use new URL()
  // directly — build the query string manually.
  const base = `${API_BASE}${path}`;
  const qs = new URLSearchParams();
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") {
        qs.set(k, String(v));
      }
    }
  }
  const query = qs.toString();
  return query ? `${base}?${query}` : base;
}

async function apiFetch<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = buildUrl(path, params);
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

async function apiMutate<T>(path: string, method: string, body?: unknown): Promise<T> {
  const url = buildUrl(path);
  const res = await fetch(url, {
    method,
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    throw new Error(`API error: ${res.status} ${res.statusText}`);
  }
  // 204 No Content has no body
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// API methods
// ---------------------------------------------------------------------------

export async function getDetections(q: DetectionQuery = {}): Promise<DetectionListResponse> {
  // Backend uses alias "class" for the object_class filter (FastAPI Query alias).
  const params: Record<string, string | number | undefined> = {
    camera_id: q.camera_id,
    start: q.start,
    end: q.end,
    class: q.object_class,
    min_confidence: q.min_confidence,
    has_thumbnail: q.has_thumbnail === undefined ? undefined : q.has_thumbnail ? "true" : "false",
    offset: q.offset,
    limit: q.limit,
  };
  return apiFetch<DetectionListResponse>("/detections", params);
}

export async function getTracks(q: TrackQuery = {}): Promise<TrackListResponse> {
  // Backend uses alias "class" for the object_class filter (FastAPI Query alias).
  const params: Record<string, string | number | undefined> = {
    camera_id: q.camera_id,
    start: q.start,
    end: q.end,
    class: q.object_class,
    state: q.state,
    offset: q.offset,
    limit: q.limit,
  };
  return apiFetch<TrackListResponse>("/tracks", params);
}

export async function getTrackDetail(localTrackId: string): Promise<TrackDetailResponse> {
  return apiFetch<TrackDetailResponse>(`/tracks/${localTrackId}`);
}

export async function getEvents(q: EventQuery = {}): Promise<EventListResponse> {
  const params: Record<string, string | number | undefined> = {
    site_id: q.site_id,
    camera_id: q.camera_id,
    start: q.start,
    end: q.end,
    event_type: q.event_type,
    state: q.state,
    has_clip: q.has_clip === undefined ? undefined : q.has_clip ? "true" : "false",
    offset: q.offset,
    limit: q.limit,
  };
  return apiFetch<EventListResponse>("/events", params);
}

export async function getTopology(siteId: string): Promise<TopologyResponse> {
  return apiFetch<TopologyResponse>(`/topology/${siteId}`);
}

export async function getTopologyGraph(siteId: string): Promise<TopologyGraph> {
  return apiFetch<TopologyGraph>(`/topology/${siteId}`);
}

export async function addCamera(siteId: string, body: CameraCreateRequest): Promise<CameraNode> {
  return apiMutate<CameraNode>(`/topology/${siteId}/cameras`, "POST", body);
}

export async function removeCamera(siteId: string, cameraId: string): Promise<void> {
  return apiMutate<void>(`/topology/${siteId}/cameras/${cameraId}`, "DELETE");
}

export async function upsertEdge(siteId: string, body: EdgeCreateRequest): Promise<TransitionEdge> {
  return apiMutate<TransitionEdge>(`/topology/${siteId}/edges`, "PUT", body);
}

// ---------------------------------------------------------------------------
// Sites
// ---------------------------------------------------------------------------

export interface SiteResponse {
  site_id: string;
  name: string;
  address: string | null;
  timezone: string;
  camera_count: number;
}

export interface SiteListResponse {
  sites: SiteResponse[];
  total: number;
}

export interface CreateSiteRequest {
  name: string;
  address: string | null;
  timezone: string;
}

export interface UpdateSiteRequest {
  name: string;
  address: string | null;
  timezone: string;
}

export async function getSites(
  q: { offset?: number; limit?: number } = {},
): Promise<SiteListResponse> {
  return apiFetch<SiteListResponse>("/sites", q as Record<string, string | number | undefined>);
}

export async function getSite(siteId: string): Promise<SiteResponse> {
  return apiFetch<SiteResponse>(`/sites/${siteId}`);
}

export async function createSite(body: CreateSiteRequest): Promise<SiteResponse> {
  return apiMutate<SiteResponse>("/sites", "POST", body);
}

export async function updateSite(siteId: string, body: UpdateSiteRequest): Promise<SiteResponse> {
  return apiMutate<SiteResponse>(`/sites/${siteId}`, "PUT", body);
}

// ---------------------------------------------------------------------------
// Service toggles (admin-controlled enable/disable of optional services)
// ---------------------------------------------------------------------------

export interface ServiceToggle {
  service_name: string;
  enabled: boolean;
  description: string | null;
  impact: string | null;
  ram_savings_mb: number | null;
  container_status: string | null;
  updated_at: string | null;
}

export async function getToggles(): Promise<{ toggles: ServiceToggle[] }> {
  const res = await fetch("/api/admin/toggles", { credentials: "include" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function setToggle(
  serviceName: string,
  enabled: boolean,
): Promise<ServiceToggle> {
  const res = await fetch(`/api/admin/toggles/${serviceName}`, {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Camera recording profiles
// ---------------------------------------------------------------------------

export interface CameraProfile {
  profile_id: string;
  name: string;
  description: string | null;
  recording_mode: "continuous" | "motion" | "hybrid";
  business_hours_start: string | null;
  business_hours_end: string | null;
  business_days: number[];
  motion_sensitivity: number;
  pre_roll_s: number;
  post_roll_s: number;
  timezone: string;
  is_default: boolean;
  cameras_assigned: number;
}

export interface CameraProfileCreate {
  name: string;
  description?: string | null;
  recording_mode: "continuous" | "motion" | "hybrid";
  business_hours_start?: string | null;
  business_hours_end?: string | null;
  business_days: number[];
  motion_sensitivity: number;
  pre_roll_s: number;
  post_roll_s: number;
  timezone: string;
}

export interface ProfileAssignment {
  camera_id: string;
  profile_id: string | null;
  profile_name: string | null;
  recording_mode: string | null;
}

async function profilesFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/admin/camera-profiles${path}`, {
    credentials: "include",
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function getCameraProfiles(): Promise<{ profiles: CameraProfile[] }> {
  return profilesFetch<{ profiles: CameraProfile[] }>("");
}

export async function getProfileAssignments(): Promise<{ assignments: ProfileAssignment[] }> {
  return profilesFetch<{ assignments: ProfileAssignment[] }>("/assignments");
}

export async function createCameraProfile(
  body: CameraProfileCreate,
): Promise<{ profile_id: string }> {
  return profilesFetch<{ profile_id: string }>("", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function updateCameraProfile(
  profileId: string,
  body: CameraProfileCreate,
): Promise<void> {
  await profilesFetch<{ status: string }>(`/${profileId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteCameraProfile(profileId: string): Promise<void> {
  await profilesFetch<{ status: string }>(`/${profileId}`, { method: "DELETE" });
}

export async function assignCameraProfile(
  cameraId: string,
  profileId: string,
): Promise<void> {
  await profilesFetch<{ status: string }>(`/assign/${cameraId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile_id: profileId }),
  });
}

// ---------------------------------------------------------------------------
// Storage tiers (hot/warm/cold video storage)
// ---------------------------------------------------------------------------

export interface TierQuality {
  width: number;
  height: number;
  fps: number;
  bitrate_kbps: number;
}

export interface StorageTierConfig {
  total_budget_gb: number;
  hot_fraction: number;
  warm_fraction: number;
  cold_fraction: number;
  hot: TierQuality;
  warm: TierQuality;
  cold: TierQuality;
  storage_backend: "volume" | "bind";
  bind_mount_path: string | null;
}

export interface ComputedRetention {
  tier: "hot" | "warm" | "cold";
  gb: number;
  hours: number;
  pretty_duration: string;
}

export interface StorageTierConfigResponse {
  config: StorageTierConfig;
  computed: ComputedRetention[];
  disk_available_gb: number;
  disk_total_gb: number;
  num_cameras: number;
}

export interface TierUsageEntry {
  bytes: number;
  segments: number;
  oldest: string | null;
  newest: string | null;
}

export type TierUsageResponse = Record<"hot" | "warm" | "cold", TierUsageEntry>;

async function tiersFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/admin/storage-tiers${path}`, {
    credentials: "include",
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function getStorageTierConfig(): Promise<StorageTierConfigResponse> {
  return tiersFetch<StorageTierConfigResponse>("");
}

export async function updateStorageTierConfig(
  body: StorageTierConfig,
): Promise<{ status: string }> {
  return tiersFetch<{ status: string }>("", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function getStorageTierUsage(): Promise<TierUsageResponse> {
  return tiersFetch<TierUsageResponse>("/usage");
}
