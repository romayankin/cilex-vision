"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Calendar } from "lucide-react";
import ClipPlayer from "@/components/ClipPlayer";

interface TimelineBlock {
  event_id: string;
  start_time: string;
  end_time: string;
  duration_ms: number;
  state: string;
  objects_summary: Record<string, { count: number }> | null;
}

interface TimelineResponse {
  date: string;
  tz_offset_minutes: number;
  utc_start: string;
  utc_end: string;
  cameras: Record<string, TimelineBlock[]>;
}

interface EventDetail {
  event_id: string;
  camera_id: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  state: string;
  clip_uri: string | null;
  clip_source_type: "standalone" | "segment_range" | null;
  metadata: unknown;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function todayLocalISO(): string {
  const d = new Date();
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDuration(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s - m * 60;
  return `${m}m ${rs}s`;
}

function summarizeObjects(objects: TimelineBlock["objects_summary"]): string {
  if (!objects) return "No objects";
  const parts: string[] = [];
  for (const [cls, info] of Object.entries(objects)) {
    const icon =
      cls === "person" ? "👤" :
      cls === "car" ? "🚗" :
      cls === "animal" ? "🐾" : "🔸";
    parts.push(`${icon} ${info.count} ${cls}${info.count !== 1 ? "s" : ""}`);
  }
  return parts.join(" · ");
}

function CameraRow({
  cameraId,
  blocks,
  utcStart,
  selectedId,
  onSelect,
}: {
  cameraId: string;
  blocks: TimelineBlock[];
  utcStart: Date;
  selectedId: string | null;
  onSelect: (block: TimelineBlock) => void;
}) {
  return (
    <div className="mb-4">
      <div className="text-sm font-medium mb-1">{cameraId}</div>
      <div className="relative h-8 bg-gray-50 border border-gray-200 rounded overflow-hidden">
        {Array.from({ length: 24 }, (_, h) => (
          <div
            key={h}
            className="absolute top-0 bottom-0 border-l border-gray-200"
            style={{ left: `${(h / 24) * 100}%` }}
          >
            <span className="absolute top-0 left-0.5 text-[9px] text-gray-400">
              {h.toString().padStart(2, "0")}
            </span>
          </div>
        ))}

        {blocks.map((block) => {
          const start = new Date(block.start_time);
          const offsetMs = start.getTime() - utcStart.getTime();
          if (offsetMs < 0 || offsetMs >= DAY_MS) return null;

          const leftPct = (offsetMs / DAY_MS) * 100;
          const durationMs = Math.max(1000, block.duration_ms || 0);
          const widthPct = Math.max(0.2, (durationMs / DAY_MS) * 100);
          const isSelected = selectedId === block.event_id;
          const isActive = block.state === "active";

          const objectLabel = summarizeObjects(block.objects_summary);

          return (
            <button
              type="button"
              key={block.event_id}
              onClick={() => onSelect(block)}
              className={`absolute top-1 bottom-1 rounded border transition ${
                isSelected
                  ? "bg-blue-600 border-blue-700 ring-2 ring-blue-300"
                  : isActive
                    ? "bg-amber-400 border-amber-500 hover:bg-amber-500"
                    : "bg-blue-400 border-blue-500 hover:bg-blue-500"
              }`}
              style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
              title={`${new Date(block.start_time).toLocaleTimeString()} · ${formatDuration(block.duration_ms)}${isActive ? " (ongoing)" : ""} · ${objectLabel}`}
            />
          );
        })}
      </div>
    </div>
  );
}

export default function TimelinePage() {
  const [date, setDate] = useState<string>(todayLocalISO);
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<TimelineBlock | null>(null);
  const [selectedCameraId, setSelectedCameraId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EventDetail | null>(null);

  const tzOffsetMinutes = useMemo(() => -new Date().getTimezoneOffset(), []);

  const utcStart = useMemo(() => {
    if (!timeline?.utc_start) return null;
    return new Date(timeline.utc_start);
  }, [timeline]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      setSelected(null);
      setDetail(null);
      try {
        const params = new URLSearchParams({
          date,
          tz_offset_minutes: String(tzOffsetMinutes),
        });
        const res = await fetch(`/api/events/timeline?${params}`, {
          credentials: "include",
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: TimelineResponse = await res.json();
        if (!cancelled) setTimeline(data);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load timeline");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [date, tzOffsetMinutes]);

  const handleSelect = useCallback(async (block: TimelineBlock, cameraId: string) => {
    setSelected(block);
    setSelectedCameraId(cameraId);
    setDetail(null);
    try {
      const res = await fetch(`/api/events/${block.event_id}`, { credentials: "include" });
      if (res.ok) {
        const data: EventDetail = await res.json();
        setDetail(data);
      }
    } catch {
      /* non-fatal, player will show error */
    }
  }, []);

  const cameras = timeline?.cameras ?? {};
  const hasAnyBlocks = Object.values(cameras).some((b) => b.length > 0);

  return (
    <div className="space-y-4 p-4">
      <div className="flex items-center gap-3">
        <Calendar className="w-5 h-5 text-gray-700" />
        <h1 className="text-xl font-semibold">Timeline</h1>
        <input
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          className="border border-gray-300 rounded px-2 py-1 text-sm"
        />
        <span className="text-xs text-gray-500">
          {loading
            ? "Loading…"
            : error
              ? <span className="text-red-600">{error}</span>
              : hasAnyBlocks
                ? `${Object.values(cameras).reduce((n, b) => n + b.length, 0)} events across ${Object.keys(cameras).length} cameras`
                : "No motion events this day"}
        </span>
      </div>

      {utcStart && Object.entries(cameras).map(([cameraId, blocks]) => (
        <CameraRow
          key={cameraId}
          cameraId={cameraId}
          blocks={blocks}
          utcStart={utcStart}
          selectedId={selected?.event_id || null}
          onSelect={(b) => handleSelect(b, cameraId)}
        />
      ))}

      {!loading && !error && !hasAnyBlocks && (
        <div className="text-center py-12 text-gray-400 text-sm">
          No motion events on {date}.
          <div className="text-xs mt-2">
            If you expect events here but see none, make sure <code className="bg-gray-100 px-1">EVENT_MOTION_EVENTS_ENABLED=true</code> in docker-compose.yml.
          </div>
        </div>
      )}

      {selected && (
        <div className="border-t border-gray-200 pt-4">
          <div className="text-sm mb-2">
            <span className="font-medium">{selectedCameraId}</span>
            {" · "}
            {new Date(selected.start_time).toLocaleTimeString()}
            {" · "}
            {formatDuration(selected.duration_ms)}
            {selected.state === "active" && (
              <span className="ml-2 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5">
                ongoing
              </span>
            )}
            <span className="ml-2 text-xs text-gray-500">
              {summarizeObjects(selected.objects_summary)}
            </span>
          </div>
          {detail ? (
            <ClipPlayer
              uri={detail.clip_uri}
              sourceType={detail.clip_source_type ?? "standalone"}
            />
          ) : (
            <div className="text-xs text-gray-400 italic">Loading clip…</div>
          )}
        </div>
      )}
    </div>
  );
}
