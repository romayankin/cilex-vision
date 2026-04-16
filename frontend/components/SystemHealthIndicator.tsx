"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 30_000;
const CRITICAL_CONTAINERS = ["inference-worker", "decode-service", "edge-agent"];
const STALE_DETECTIONS_WARN_S = 5 * 60;
const STALE_DETECTIONS_CRIT_S = 15 * 60;

type HealthState = "healthy" | "degraded" | "down" | "unknown";

interface Camera {
  camera_id: string;
  name: string;
  status: string;
  detections_5min: number;
  total_tracks: number;
}

interface PipelineStatus {
  containers: Record<string, string>;
  database: {
    detections_last_5min: number;
    latest_detection: string | null;
    total_detections: number;
  };
  cameras: Camera[];
}

export default function SystemHealthIndicator() {
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/pipeline/status", { credentials: "include" });
      if (!res.ok) {
        setFetchError(`HTTP ${res.status}`);
        return;
      }
      const data: PipelineStatus = await res.json();
      setStatus(data);
      setFetchError(null);
    } catch (err) {
      setFetchError(err instanceof Error ? err.message : "fetch failed");
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const id = window.setInterval(fetchStatus, POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [fetchStatus]);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  const health = computeHealth(status, fetchError);
  const { dotClass, label } = presentHealth(health);

  return (
    <div className="relative" ref={dropdownRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 px-2 py-1 rounded text-xs font-medium hover:bg-gray-100 transition-colors"
        title="System health"
      >
        <span className={`inline-block w-2 h-2 rounded-full ${dotClass}`} />
        <span className="text-gray-600">{label}</span>
      </button>

      {open && (
        <div className="absolute left-0 top-full mt-1 w-80 bg-white border border-gray-200 rounded-lg shadow-lg z-50 text-sm">
          <HealthDetails status={status} error={fetchError} />
        </div>
      )}
    </div>
  );
}

function computeHealth(status: PipelineStatus | null, error: string | null): HealthState {
  if (error) return "unknown";
  if (!status) return "unknown";

  for (const name of CRITICAL_CONTAINERS) {
    const st = status.containers[name];
    if (st && st !== "up") return "down";
  }

  const latest = status.database.latest_detection;
  if (!latest) return "down";

  const ageS = (Date.now() - new Date(latest).getTime()) / 1000;
  if (ageS >= STALE_DETECTIONS_CRIT_S) return "down";
  if (ageS >= STALE_DETECTIONS_WARN_S) return "degraded";

  for (const [name, st] of Object.entries(status.containers)) {
    if (!CRITICAL_CONTAINERS.includes(name) && st !== "up") return "degraded";
  }

  return "healthy";
}

function presentHealth(state: HealthState): { dotClass: string; label: string } {
  switch (state) {
    case "healthy":
      return { dotClass: "bg-green-500", label: "Healthy" };
    case "degraded":
      return { dotClass: "bg-amber-500 animate-pulse", label: "Degraded" };
    case "down":
      return { dotClass: "bg-red-500 animate-pulse", label: "Issue" };
    default:
      return { dotClass: "bg-gray-400", label: "—" };
  }
}

function formatAge(iso: string | null): string {
  if (!iso) return "never";
  const ageS = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (ageS < 60) return `${ageS}s ago`;
  if (ageS < 3600) return `${Math.floor(ageS / 60)}m ago`;
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ${Math.floor((ageS % 3600) / 60)}m ago`;
  return `${Math.floor(ageS / 86400)}d ago`;
}

function HealthDetails({
  status,
  error,
}: {
  status: PipelineStatus | null;
  error: string | null;
}) {
  if (error) {
    return (
      <div className="p-4">
        <div className="text-red-700 font-medium mb-2">Cannot reach backend</div>
        <div className="text-xs text-gray-500">{error}</div>
        <div className="text-xs text-gray-400 mt-2">
          Either the query-api is down, or you are not authenticated as admin.
        </div>
      </div>
    );
  }
  if (!status) {
    return <div className="p-4 text-gray-400">Loading…</div>;
  }

  const latestAge = formatAge(status.database.latest_detection);
  const containers = Object.entries(status.containers);
  const downContainers = containers.filter(([, st]) => st !== "up");

  return (
    <div className="p-3 space-y-3">
      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">
          Detections
        </div>
        <div className="flex items-baseline justify-between">
          <span className="font-mono">{status.database.detections_last_5min.toLocaleString()}</span>
          <span className="text-xs text-gray-400">last 5 min</span>
        </div>
        <div className="flex items-baseline justify-between mt-1">
          <span className="text-xs text-gray-600">Most recent:</span>
          <span className="text-xs font-mono text-gray-700">{latestAge}</span>
        </div>
      </div>

      {status.cameras.length > 0 && (
        <div>
          <div className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">
            Cameras
          </div>
          <div className="space-y-1">
            {status.cameras.map((cam) => {
              const active = cam.detections_5min > 0;
              return (
                <div key={cam.camera_id} className="flex items-center justify-between text-xs">
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span
                      className={`inline-block w-1.5 h-1.5 rounded-full ${
                        active ? "bg-green-500" : "bg-gray-300"
                      }`}
                    />
                    <span className="truncate">{cam.name || cam.camera_id}</span>
                  </div>
                  <span className="font-mono text-gray-600 ml-2">
                    {cam.detections_5min.toLocaleString()}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div>
        <div className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">
          Services
        </div>
        {downContainers.length === 0 ? (
          <div className="text-xs text-green-700">All {containers.length} services up</div>
        ) : (
          <div className="space-y-1">
            {containers.map(([name, st]) => {
              const up = st === "up";
              return (
                <div key={name} className="flex items-center justify-between text-xs">
                  <span className="truncate">{name}</span>
                  <span
                    className={`font-mono ${
                      up ? "text-green-700" : "text-red-700 font-semibold"
                    }`}
                  >
                    {st}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <div className="border-t border-gray-100 pt-2">
        <a href="/admin/pipeline" className="text-xs text-blue-600 hover:underline">
          View pipeline diagnostics →
        </a>
      </div>
    </div>
  );
}
