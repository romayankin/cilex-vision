"use client";

import { useEffect, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface CameraRow {
  camera_id: string;
  name: string;
  status: string;
  detections_5min: number;
  total_tracks: number;
}

interface PipelineStatus {
  stages: {
    edge_agent: Record<string, number | string>;
    ingress_bridge: Record<string, number | string>;
    decode_service: Record<string, number | string>;
    inference_worker: Record<string, number | string>;
    bulk_collector: Record<string, number | string>;
  };
  containers: Record<string, string>;
  database: {
    total_detections: number;
    detections_last_5min: number;
    total_tracks: number;
    active_tracks: number;
    total_events: number;
    latest_detection: string | null;
  };
  cameras: CameraRow[];
}

const REFRESH_MS = 5000;

// Sum all series values whose label-free name starts with `prefix`.
// Prometheus per-camera counters show up as e.g.
// "edge_motion_frames_total{camera_id=\"cam-1\"}" — summing collapses labels.
function sumMetric(
  metrics: Record<string, number | string>,
  prefix: string,
): number {
  let total = 0;
  for (const [key, val] of Object.entries(metrics)) {
    if (typeof val !== "number") continue;
    const bare = key.split("{")[0];
    if (bare === prefix) total += val;
  }
  return total;
}

function formatNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

function secondsAgo(iso: string | null): string {
  if (!iso) return "never";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0) return "just now";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function StatusDot({ status }: { status: string }) {
  const color =
    status === "up" ? "bg-green-500" :
    status === "down" ? "bg-red-500" :
    "bg-yellow-500";
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full ${color} ${
        status === "up" ? "animate-pulse" : ""
      }`}
    />
  );
}

function StageBox({
  title,
  status,
  lines,
}: {
  title: string;
  status: string;
  lines: { label: string; value: string }[];
}) {
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3 min-w-[150px] flex-shrink-0">
      <div className="flex items-center gap-2 mb-2">
        <StatusDot status={status} />
        <span className="font-medium text-sm">{title}</span>
      </div>
      <div className="text-xs text-gray-600 space-y-0.5">
        {lines.map((l) => (
          <div key={l.label} className="flex justify-between gap-3">
            <span className="text-gray-400">{l.label}</span>
            <span className="font-mono">{l.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Arrow({ active }: { active: boolean }) {
  return (
    <div
      className={`flex items-center px-1 text-2xl leading-none select-none ${
        active ? "text-green-500 animate-pulse" : "text-gray-300"
      }`}
    >
      →
    </div>
  );
}

function StatCard({
  label,
  value,
  hint,
  accent = false,
}: {
  label: string;
  value: string;
  hint?: string;
  accent?: boolean;
}) {
  return (
    <div
      className={`bg-white border rounded-lg p-4 ${
        accent ? "border-blue-300 ring-1 ring-blue-100" : "border-gray-200"
      }`}
    >
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${accent ? "text-blue-700" : ""}`}>
        {value}
      </div>
      {hint && <div className="text-xs text-gray-400 mt-1">{hint}</div>}
    </div>
  );
}

export default function PipelinePage() {
  const role = getUserRole();
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFetch, setLastFetch] = useState<number>(Date.now());

  useEffect(() => {
    if (!isAdmin(role)) return;
    let cancelled = false;

    async function load() {
      try {
        const res = await fetch("/api/pipeline/status", { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: PipelineStatus = await res.json();
        if (!cancelled) {
          setStatus(data);
          setError(null);
          setLastFetch(Date.now());
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Fetch failed");
      }
    }

    load();
    const id = setInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [role]);

  // "Now" tick so "Last updated" counter advances without refetching.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  if (!status && !error) {
    return <div className="text-sm text-gray-400">Loading pipeline status…</div>;
  }

  const s = status;
  const containers = s?.containers ?? {};
  const db = s?.database;

  const edgeFrames = s ? sumMetric(s.stages.edge_agent, "edge_motion_frames_total") : 0;
  const edgeFiltered = s
    ? sumMetric(s.stages.edge_agent, "edge_static_frames_filtered_total")
    : 0;
  const bridgeReceived = s
    ? sumMetric(s.stages.ingress_bridge, "bridge_messages_received_total")
    : 0;
  const bridgeProduced = s
    ? sumMetric(s.stages.ingress_bridge, "bridge_messages_produced_total")
    : 0;
  const decodeConsumed = s
    ? sumMetric(s.stages.decode_service, "decode_frames_consumed_total")
    : 0;
  const decodeDecoded = s
    ? sumMetric(s.stages.decode_service, "decode_frames_decoded_total")
    : 0;
  const infFrames = s
    ? sumMetric(s.stages.inference_worker, "inference_frames_consumed_total")
    : 0;
  const infLatencyCount = s
    ? sumMetric(s.stages.inference_worker, "inference_latency_ms_count")
    : 0;
  const infLatencySum = s
    ? sumMetric(s.stages.inference_worker, "inference_latency_ms_sum")
    : 0;
  const infAvgMs = infLatencyCount > 0 ? infLatencySum / infLatencyCount : 0;
  const bulkStaged = s ? sumMetric(s.stages.bulk_collector, "bulk_rows_staged") : 0;

  const flowing = (db?.detections_last_5min ?? 0) > 0;
  const secSinceFetch = Math.floor((Date.now() - lastFetch) / 1000);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Pipeline Monitor</h1>
        <div className="text-xs text-gray-500">
          {error ? (
            <span className="text-red-600">Error: {error}</span>
          ) : (
            <span>Last updated: {secSinceFetch}s ago · refresh every {REFRESH_MS / 1000}s</span>
          )}
        </div>
      </div>

      {/* Flow diagram */}
      <section className="bg-gray-50 border border-gray-200 rounded-lg p-4 overflow-x-auto">
        <div className="flex items-stretch gap-1 min-w-max">
          <StageBox
            title="Edge Agent"
            status={containers["edge-agent"] ?? "down"}
            lines={[
              { label: "motion", value: formatNumber(edgeFrames) },
              { label: "filtered", value: formatNumber(edgeFiltered) },
            ]}
          />
          <Arrow active={flowing && edgeFrames > 0} />
          <StageBox
            title="Ingress Bridge"
            status={containers["ingress-bridge"] ?? "down"}
            lines={[
              { label: "recv", value: formatNumber(bridgeReceived) },
              { label: "prod", value: formatNumber(bridgeProduced) },
            ]}
          />
          <Arrow active={flowing && bridgeProduced > 0} />
          <StageBox
            title="Decode"
            status={containers["decode-service"] ?? "down"}
            lines={[
              { label: "in", value: formatNumber(decodeConsumed) },
              { label: "decoded", value: formatNumber(decodeDecoded) },
            ]}
          />
          <Arrow active={flowing && decodeDecoded > 0} />
          <StageBox
            title="Inference"
            status={containers["inference-worker"] ?? "down"}
            lines={[
              { label: "frames", value: formatNumber(infFrames) },
              { label: "avg ms", value: infAvgMs.toFixed(1) },
            ]}
          />
          <Arrow active={flowing && infFrames > 0} />
          <StageBox
            title="Bulk Collector"
            status={containers["bulk-collector"] ?? "down"}
            lines={[{ label: "staged", value: formatNumber(bulkStaged) }]}
          />
          <Arrow active={flowing} />
          <StageBox
            title="Database"
            status="up"
            lines={[
              { label: "detections", value: formatNumber(db?.total_detections ?? 0) },
              { label: "events", value: formatNumber(db?.total_events ?? 0) },
            ]}
          />
        </div>
      </section>

      {/* Stat cards */}
      <section className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <StatCard
          label="Detections (5 min)"
          value={formatNumber(db?.detections_last_5min ?? 0)}
          hint="pipeline pulse"
          accent
        />
        <StatCard label="Total detections" value={formatNumber(db?.total_detections ?? 0)} />
        <StatCard label="Active tracks" value={formatNumber(db?.active_tracks ?? 0)} hint={`${formatNumber(db?.total_tracks ?? 0)} total`} />
        <StatCard label="Total events" value={formatNumber(db?.total_events ?? 0)} />
        <StatCard
          label="Latest detection"
          value={secondsAgo(db?.latest_detection ?? null)}
        />
        <StatCard
          label="Inference speed"
          value={infLatencyCount > 0 ? `${infAvgMs.toFixed(1)} ms` : "—"}
          hint={`${formatNumber(infLatencyCount)} frames`}
        />
      </section>

      {/* Per-camera breakdown */}
      <section>
        <h2 className="text-sm font-medium mb-2">Per-camera activity</h2>
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-xs text-gray-500 uppercase">
              <tr>
                <th className="text-left px-3 py-2">Camera</th>
                <th className="text-left px-3 py-2">Edge motion</th>
                <th className="text-left px-3 py-2">Detections (5 min)</th>
                <th className="text-left px-3 py-2">Tracks</th>
                <th className="text-left px-3 py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {(s?.cameras ?? []).map((c) => {
                const camMotion = s
                  ? sumMetric(
                      Object.fromEntries(
                        Object.entries(s.stages.edge_agent).filter(([k]) =>
                          k.startsWith("edge_motion_frames_total") && k.includes(`"${c.camera_id}"`),
                        ),
                      ),
                      "edge_motion_frames_total",
                    )
                  : 0;
                const active = c.status === "online" && c.detections_5min > 0;
                return (
                  <tr key={c.camera_id} className="border-t border-gray-100">
                    <td className="px-3 py-2">
                      <div className="font-medium">{c.name || c.camera_id}</div>
                      <div className="text-xs text-gray-400">{c.camera_id}</div>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {formatNumber(camMotion)}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {formatNumber(c.detections_5min)}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {formatNumber(c.total_tracks)}
                    </td>
                    <td className="px-3 py-2">
                      <span className="inline-flex items-center gap-2 text-xs">
                        <StatusDot status={active ? "up" : c.status === "online" ? "error" : "down"} />
                        {active ? "active" : c.status}
                      </span>
                    </td>
                  </tr>
                );
              })}
              {(s?.cameras ?? []).length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-6 text-center text-gray-400 text-xs">
                    No cameras registered.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
