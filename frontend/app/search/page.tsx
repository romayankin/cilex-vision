"use client";

import { useEffect, useMemo, useState } from "react";
import { Camera, Clock, Download, MapPin, Play, Search as SearchIcon } from "lucide-react";
import ClipPlayer from "@/components/ClipPlayer";
import type { EventMetadata } from "@/lib/event-metadata";

interface MotionEvent {
  event_id: string;
  camera_id: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  clip_uri: string | null;
  clip_source_type: "standalone" | "segment_range" | null;
  metadata: EventMetadata | null;
}

interface ApiEventResponse {
  event_id: string;
  camera_id: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  clip_uri: string | null;
  clip_source_type: "standalone" | "segment_range" | null;
  metadata: EventMetadata | null;
}

interface ApiListResponse {
  events: ApiEventResponse[];
  total: number;
  offset: number;
  limit: number;
}

interface Filters {
  cameraId: string;
  timeRange: "1h" | "24h" | "7d" | "30d" | "all";
  containsPerson: boolean;
  containsCar: boolean;
  minDurationS: string;
  maxDurationS: string;
}

const DEFAULT_FILTERS: Filters = {
  cameraId: "",
  timeRange: "24h",
  containsPerson: false,
  containsCar: false,
  minDurationS: "",
  maxDurationS: "",
};

function computeTimeRange(range: Filters["timeRange"]): { start?: string; end?: string } {
  if (range === "all") return {};
  const ms: Record<string, number> = {
    "1h": 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
    "30d": 30 * 24 * 60 * 60 * 1000,
  };
  const now = Date.now();
  return { start: new Date(now - ms[range]).toISOString(), end: new Date(now).toISOString() };
}

function formatDuration(sec: number): string {
  if (!isFinite(sec) || sec <= 0) return "—";
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec - m * 60);
  return `${m}m ${s}s`;
}

function objectIcon(cls: string): string {
  if (cls === "person") return "👤";
  if (cls === "car" || cls === "truck" || cls === "bus") return "🚗";
  if (cls === "animal" || cls === "dog" || cls === "cat") return "🐾";
  return "🔸";
}

function summarizeObjects(metadata: EventMetadata | null): string[] {
  if (!metadata?.objects) return [];
  return Object.entries(metadata.objects).map(([cls, info]) => {
    const attrs: string[] = [];
    if (info.attributes?.upper_colors?.length) {
      attrs.push(`${info.attributes.upper_colors.join("/")} upper`);
    }
    if (info.attributes?.lower_colors?.length) {
      attrs.push(`${info.attributes.lower_colors.join("/")} lower`);
    }
    if (info.attributes?.colors?.length) {
      attrs.push(info.attributes.colors.join("/"));
    }
    const attrsStr = attrs.length ? ` (${attrs.join(", ")})` : "";
    return `${objectIcon(cls)} ${info.count} ${cls}${info.count !== 1 ? "s" : ""}${attrsStr}`;
  });
}

function standaloneDownloadUrl(uri: string): string {
  const key = uri.replace(/^s3:\/\/event-clips\//, "");
  const encoded = key.split("/").map(encodeURIComponent).join("/");
  return `/api/clips/s3/${encoded}`;
}

export default function SearchPage() {
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [cameras, setCameras] = useState<string[]>([]);
  const [results, setResults] = useState<MotionEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/streams", { credentials: "include" })
      .then((r) => r.json())
      .then((d: { streams?: { camera_id: string }[] }) => {
        setCameras((d.streams ?? []).map((c) => c.camera_id));
      })
      .catch(() => {});
  }, []);

  const query = useMemo(() => {
    const params = new URLSearchParams();
    params.set("event_type", "motion");
    if (filters.cameraId) params.set("camera_id", filters.cameraId);
    const { start, end } = computeTimeRange(filters.timeRange);
    if (start) params.set("start", start);
    if (end) params.set("end", end);
    if (filters.containsPerson) params.append("contains_classes", "person");
    if (filters.containsCar) params.append("contains_classes", "car");
    if (filters.minDurationS) params.set("min_duration_s", filters.minDurationS);
    if (filters.maxDurationS) params.set("max_duration_s", filters.maxDurationS);
    params.set("limit", "50");
    return params.toString();
  }, [filters]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`/api/events?${query}`, { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: ApiListResponse = await res.json();
        if (!cancelled) setResults(data.events);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Search failed");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [query]);

  return (
    <div className="space-y-4 p-4">
      <div className="bg-white border border-gray-200 rounded-lg p-4 space-y-3">
        <h2 className="text-sm font-semibold text-gray-900 flex items-center gap-1.5">
          <SearchIcon className="w-4 h-4" /> Search filters
        </h2>

        <div className="flex flex-wrap gap-4 items-center">
          <label className="text-sm">
            Camera:
            <select
              value={filters.cameraId}
              onChange={(e) => setFilters({ ...filters, cameraId: e.target.value })}
              className="ml-1 border border-gray-300 rounded px-2 py-0.5 text-sm"
            >
              <option value="">All</option>
              {cameras.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>

          <label className="text-sm">
            Time:
            <select
              value={filters.timeRange}
              onChange={(e) =>
                setFilters({ ...filters, timeRange: e.target.value as Filters["timeRange"] })
              }
              className="ml-1 border border-gray-300 rounded px-2 py-0.5 text-sm"
            >
              <option value="1h">Last 1 hour</option>
              <option value="24h">Last 24 hours</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
              <option value="all">All time</option>
            </select>
          </label>

          <label className="text-sm flex items-center gap-1">
            <input
              type="checkbox"
              checked={filters.containsPerson}
              onChange={(e) => setFilters({ ...filters, containsPerson: e.target.checked })}
            />
            👤 person
          </label>

          <label className="text-sm flex items-center gap-1">
            <input
              type="checkbox"
              checked={filters.containsCar}
              onChange={(e) => setFilters({ ...filters, containsCar: e.target.checked })}
            />
            🚗 car
          </label>

          <label className="text-sm">
            Duration:
            <input
              type="number"
              placeholder="min"
              value={filters.minDurationS}
              onChange={(e) => setFilters({ ...filters, minDurationS: e.target.value })}
              className="ml-1 w-16 border border-gray-300 rounded px-1 py-0.5 text-sm"
            />
            <span className="text-gray-400 mx-1">–</span>
            <input
              type="number"
              placeholder="max"
              value={filters.maxDurationS}
              onChange={(e) => setFilters({ ...filters, maxDurationS: e.target.value })}
              className="w-16 border border-gray-300 rounded px-1 py-0.5 text-sm"
            />
            <span className="text-xs text-gray-500 ml-1">sec</span>
          </label>
        </div>
      </div>

      <div className="space-y-2">
        <div className="text-xs text-gray-500">
          {loading
            ? "Loading…"
            : error
              ? <span className="text-red-600">{error}</span>
              : `${results.length} results`}
        </div>

        {results.map((ev) => {
          const durationS = ev.duration_ms ? ev.duration_ms / 1000 : 0;
          const objectLines = summarizeObjects(ev.metadata);
          const isExpanded = expanded === ev.event_id;
          const zones = ev.metadata?.zones_triggered ?? [];

          return (
            <div
              key={ev.event_id}
              className="border border-gray-200 rounded-lg p-3 bg-white hover:border-blue-300 transition"
            >
              <div className="flex items-center gap-3 mb-1 text-xs flex-wrap">
                <span className="font-mono text-gray-600">
                  {new Date(ev.start_time).toLocaleString()}
                </span>
                <span className="bg-gray-100 text-gray-700 px-2 py-0.5 rounded flex items-center gap-1">
                  <Camera className="w-3 h-3" /> {ev.camera_id}
                </span>
                <span className="bg-gray-100 text-gray-700 px-2 py-0.5 rounded flex items-center gap-1">
                  <Clock className="w-3 h-3" /> {formatDuration(durationS)}
                </span>
                {zones.length > 0 && (
                  <span className="bg-blue-50 text-blue-700 px-2 py-0.5 rounded flex items-center gap-1">
                    <MapPin className="w-3 h-3" /> {zones.join(", ")}
                  </span>
                )}
              </div>

              <div className="text-sm text-gray-800 space-y-0.5 mb-2">
                {objectLines.length === 0 ? (
                  <div className="text-gray-400 italic">No objects detected</div>
                ) : (
                  objectLines.map((line, i) => <div key={i}>{line}</div>)
                )}
              </div>

              <div className="flex gap-2 items-center">
                <button
                  type="button"
                  onClick={() => setExpanded(isExpanded ? null : ev.event_id)}
                  disabled={!ev.clip_uri}
                  className="flex items-center gap-1 text-xs bg-blue-600 text-white px-3 py-1 rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
                >
                  <Play className="w-3 h-3" /> {isExpanded ? "Hide" : "Play"}
                </button>
                {ev.clip_uri && ev.clip_source_type === "standalone" && (
                  <a
                    href={standaloneDownloadUrl(ev.clip_uri)}
                    download
                    className="flex items-center gap-1 text-xs border border-gray-300 text-gray-700 px-3 py-1 rounded hover:bg-gray-50"
                  >
                    <Download className="w-3 h-3" /> Download
                  </a>
                )}
                {!ev.clip_uri && (
                  <span className="text-xs text-gray-400 italic">
                    No clip (recorder was not running)
                  </span>
                )}
              </div>

              {isExpanded && (
                <div className="mt-3">
                  <ClipPlayer uri={ev.clip_uri} sourceType={ev.clip_source_type} />
                </div>
              )}
            </div>
          );
        })}

        {!loading && !error && results.length === 0 && (
          <div className="text-center py-8 text-gray-400 text-sm">
            No motion events match your filters.
          </div>
        )}
      </div>
    </div>
  );
}
