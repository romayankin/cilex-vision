"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface LatencyBucket {
  le: string;
  count: number;
}

interface Metrics {
  latency_buckets?: LatencyBucket[];
  latency_count?: number;
  latency_sum_ms?: number;
  latency_avg_ms?: number;
  embedding_count?: number;
  embedding_sum_ms?: number;
  embedding_avg_ms?: number;
  detections_by_class?: Record<string, number>;
  detections_total?: number;
  avg_detections_per_frame?: number;
  frames_consumed?: number;
  tracks_active?: Record<string, number>;
  tracks_closed?: Record<string, number>;
  publish_errors?: Record<string, number>;
  consumer_lag?: { topic: string; partition: string; lag: number }[];
  model_name?: string;
  confidence_threshold?: number;
  nms_iou_threshold?: number;
  inference_mode?: string;
  thumbnail_max_per_track?: number;
  started_at?: number;
}

const POLL_MS = 5000;

function fmtInt(n: number | undefined): string {
  if (n === undefined || n === null) return "—";
  return Math.round(n).toLocaleString();
}

function fmtUptime(startedAt: number | undefined): string {
  if (!startedAt) return "—";
  const sec = Math.floor(Date.now() / 1000 - startedAt);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ${sec % 60}s`;
  const hr = Math.floor(min / 60);
  return `${hr}h ${min % 60}m`;
}

function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wide">{label}</div>
      <div className="mt-1 text-2xl font-mono font-semibold text-gray-900">
        {value}
      </div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  );
}

function LatencyHistogram({ buckets }: { buckets: LatencyBucket[] }) {
  // Convert cumulative bucket counts into per-bucket counts.
  const sorted = [...buckets]
    .map((b) => ({ le: b.le, count: b.count }))
    .sort((a, b) => {
      const pa = a.le === "+Inf" ? Infinity : parseFloat(a.le);
      const pb = b.le === "+Inf" ? Infinity : parseFloat(b.le);
      return pa - pb;
    });
  const bars: { label: string; count: number }[] = [];
  let prev = 0;
  for (const b of sorted) {
    const delta = Math.max(0, b.count - prev);
    prev = b.count;
    const label = b.le === "+Inf" ? "∞" : `≤${b.le}`;
    bars.push({ label, count: delta });
  }
  const max = Math.max(1, ...bars.map((b) => b.count));

  if (sorted.every((b) => b.count === 0)) {
    return (
      <div className="text-xs text-gray-400 py-12 text-center">
        No inference samples yet
      </div>
    );
  }

  return (
    <div className="space-y-1">
      {bars.map((b) => (
        <div key={b.label} className="flex items-center gap-2 text-xs">
          <div className="w-16 font-mono text-right text-gray-600">
            {b.label} ms
          </div>
          <div className="flex-1 bg-gray-100 rounded h-4 overflow-hidden">
            <div
              className="h-full bg-indigo-500 transition-all duration-500"
              style={{ width: `${(b.count / max) * 100}%` }}
            />
          </div>
          <div className="w-16 font-mono text-right text-gray-700">
            {fmtInt(b.count)}
          </div>
        </div>
      ))}
    </div>
  );
}

function HorizontalBars({
  data,
  color = "bg-blue-500",
  emptyLabel,
}: {
  data: Record<string, number>;
  color?: string;
  emptyLabel: string;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    return (
      <div className="text-xs text-gray-400 py-12 text-center">{emptyLabel}</div>
    );
  }
  const max = Math.max(1, ...entries.map(([, v]) => v));
  return (
    <div className="space-y-1">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-center gap-2 text-xs">
          <div className="w-24 font-mono text-right text-gray-700 truncate">
            {k}
          </div>
          <div className="flex-1 bg-gray-100 rounded h-4 overflow-hidden">
            <div
              className={`h-full ${color} transition-all duration-500`}
              style={{ width: `${(v / max) * 100}%` }}
            />
          </div>
          <div className="w-20 font-mono text-right text-gray-700">
            {fmtInt(v)}
          </div>
        </div>
      ))}
    </div>
  );
}

function Sparkline({
  values,
  color = "#6366f1",
}: {
  values: number[];
  color?: string;
}) {
  if (values.length < 2) {
    return (
      <div className="h-8 text-[10px] text-gray-400 flex items-end">
        gathering…
      </div>
    );
  }
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const W = 120;
  const H = 32;
  const step = W / (values.length - 1);
  const points = values
    .map((v, i) => `${i * step},${H - ((v - min) / range) * H}`)
    .join(" ");
  return (
    <svg width={W} height={H} className="overflow-visible">
      <polyline
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        points={points}
      />
    </svg>
  );
}

export default function InferencePage() {
  const role = getUserRole();
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFetch, setLastFetch] = useState<number>(Date.now());
  const historyRef = useRef<
    {
      t: number;
      latency_avg_ms: number;
      avg_detections_per_frame: number;
      frames_consumed: number;
    }[]
  >([]);
  const [, setTick] = useState(0);

  const fetchOnce = useMemo(
    () => async () => {
      try {
        const res = await fetch("/api/inference/metrics", {
          credentials: "include",
        });
        if (!res.ok) {
          setError(`HTTP ${res.status}`);
          return;
        }
        const data: Metrics = await res.json();
        setMetrics(data);
        setError(null);
        setLastFetch(Date.now());
        historyRef.current.push({
          t: Date.now(),
          latency_avg_ms: data.latency_avg_ms ?? 0,
          avg_detections_per_frame: data.avg_detections_per_frame ?? 0,
          frames_consumed: data.frames_consumed ?? 0,
        });
        if (historyRef.current.length > 60) historyRef.current.shift();
        setTick((t) => t + 1);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Fetch failed");
      }
    },
    [],
  );

  useEffect(() => {
    if (!isAdmin(role)) return;
    fetchOnce();
    const id = setInterval(fetchOnce, POLL_MS);
    return () => clearInterval(id);
  }, [role, fetchOnce]);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const framesPerSecHistory = useMemo(() => {
    const h = historyRef.current;
    if (h.length < 2) return [] as number[];
    const out: number[] = [];
    for (let i = 1; i < h.length; i++) {
      const dt = (h[i].t - h[i - 1].t) / 1000;
      const df = h[i].frames_consumed - h[i - 1].frames_consumed;
      out.push(dt > 0 ? Math.max(0, df / dt) : 0);
    }
    return out;
  }, [metrics]);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  const secAgo = Math.floor((Date.now() - lastFetch) / 1000);
  const latencyHistory = historyRef.current.map((x) => x.latency_avg_ms);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Model Performance</h1>
          <div className="text-xs text-gray-500 mt-0.5">
            {metrics?.model_name ?? "—"}
            {metrics?.inference_mode ? ` · ${metrics.inference_mode}` : ""}
          </div>
        </div>
        <div className="text-xs text-gray-500">
          {error ? (
            <span className="text-red-600">Error: {error}</span>
          ) : (
            <span className="inline-flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              Live · polled {secAgo}s ago
            </span>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard
          label="Avg latency"
          value={`${(metrics?.latency_avg_ms ?? 0).toFixed(1)} ms`}
          sub={`over ${fmtInt(metrics?.latency_count)} inferences`}
        />
        <StatCard
          label="Frames consumed"
          value={fmtInt(metrics?.frames_consumed)}
          sub={
            framesPerSecHistory.length
              ? `~${framesPerSecHistory[framesPerSecHistory.length - 1].toFixed(1)} fps`
              : undefined
          }
        />
        <StatCard
          label="Total detections"
          value={fmtInt(metrics?.detections_total)}
        />
        <StatCard
          label="Det / frame"
          value={(metrics?.avg_detections_per_frame ?? 0).toFixed(2)}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <section className="bg-white border border-gray-200 rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-medium text-sm">Inference latency histogram</h2>
            <Sparkline values={latencyHistory} />
          </div>
          <LatencyHistogram buckets={metrics?.latency_buckets ?? []} />
          <div className="text-[11px] text-gray-400 mt-3">
            Buckets are cumulative in Prometheus — shown here as per-bucket
            counts (requests that fell into each range).
          </div>
        </section>

        <section className="bg-white border border-gray-200 rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="font-medium text-sm">Detections by class</h2>
            <Sparkline
              values={historyRef.current.map((x) => x.avg_detections_per_frame)}
              color="#10b981"
            />
          </div>
          <HorizontalBars
            data={metrics?.detections_by_class ?? {}}
            color="bg-blue-500"
            emptyLabel="No detections yet"
          />
        </section>

        <section className="bg-white border border-gray-200 rounded-lg p-4">
          <h2 className="font-medium text-sm mb-3">Active tracks by camera</h2>
          <HorizontalBars
            data={metrics?.tracks_active ?? {}}
            color="bg-emerald-500"
            emptyLabel="No active tracks"
          />
          {metrics?.tracks_closed &&
            Object.keys(metrics.tracks_closed).length > 0 && (
              <div className="mt-4 pt-3 border-t border-gray-100">
                <div className="text-xs text-gray-500 mb-2">
                  Tracks closed (lifetime total)
                </div>
                <HorizontalBars
                  data={metrics.tracks_closed}
                  color="bg-gray-400"
                  emptyLabel="—"
                />
              </div>
            )}
        </section>

        <section className="bg-white border border-gray-200 rounded-lg p-4 space-y-2 text-sm">
          <h2 className="font-medium">Config</h2>
          <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
            <span className="text-gray-500">Model</span>
            <span className="font-mono">{metrics?.model_name ?? "—"}</span>
            <span className="text-gray-500">Mode</span>
            <span className="font-mono">{metrics?.inference_mode ?? "—"}</span>
            <span className="text-gray-500">Confidence</span>
            <span className="font-mono">
              {metrics?.confidence_threshold?.toFixed(2) ?? "—"}
            </span>
            <span className="text-gray-500">NMS IoU</span>
            <span className="font-mono">
              {metrics?.nms_iou_threshold?.toFixed(2) ?? "—"}
            </span>
            <span className="text-gray-500">Thumbnails / track</span>
            <span className="font-mono">
              {metrics?.thumbnail_max_per_track ?? "—"}
            </span>
            <span className="text-gray-500">Avg embedding</span>
            <span className="font-mono">
              {(metrics?.embedding_avg_ms ?? 0).toFixed(1)} ms
            </span>
            <span className="text-gray-500">Uptime</span>
            <span className="font-mono">{fmtUptime(metrics?.started_at)}</span>
          </div>

          {metrics?.publish_errors &&
            Object.keys(metrics.publish_errors).length > 0 && (
              <div className="mt-3 pt-3 border-t border-gray-100">
                <div className="text-xs text-red-600 mb-1">
                  Publish errors
                </div>
                <div className="text-xs font-mono">
                  {Object.entries(metrics.publish_errors).map(([t, v]) => (
                    <div key={t}>
                      {t}: {fmtInt(v)}
                    </div>
                  ))}
                </div>
              </div>
            )}
        </section>
      </div>

      {metrics?.consumer_lag && metrics.consumer_lag.length > 0 && (
        <section className="bg-white border border-gray-200 rounded-lg p-4">
          <h2 className="font-medium text-sm mb-3">Kafka consumer lag</h2>
          <table className="w-full text-xs">
            <thead className="text-gray-500">
              <tr>
                <th className="text-left py-1">Topic</th>
                <th className="text-left py-1">Partition</th>
                <th className="text-right py-1">Lag</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {metrics.consumer_lag.map((l, i) => (
                <tr key={`${l.topic}-${l.partition}-${i}`} className="border-t border-gray-100">
                  <td className="py-1">{l.topic}</td>
                  <td className="py-1">{l.partition}</td>
                  <td className={`py-1 text-right ${l.lag > 1000 ? "text-red-600" : ""}`}>
                    {fmtInt(l.lag)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
