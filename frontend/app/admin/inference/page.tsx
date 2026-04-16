"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

const POLL_MS = 5000;
const HISTORY_SIZE = 60; // 60 × 5s = 5 min rolling window

type ServiceMetrics = Record<string, number> & { error?: string };

interface MetricsResponse {
  ts: number;
  services: Record<string, ServiceMetrics>;
}

interface KafkaGroup {
  topic: string;
  label: string;
}

interface PollSample {
  ts: number; // server timestamp (seconds)
  services: Record<string, ServiceMetrics>;
}

// ---------- Capacity ceilings (used to normalize values to 0-100) ----------

interface Ceiling {
  ceiling: number;
  unit: string;
}

const RATE_CEILINGS: Record<string, Ceiling> = {
  edge_agent: { ceiling: 60, unit: "fps" },
  ingress_bridge: { ceiling: 60, unit: "fps" },
  decode_service: { ceiling: 15, unit: "fps" },
  inference_worker: { ceiling: 8, unit: "fps" },
  event_engine: { ceiling: 20, unit: "eps" },
  clip_service: { ceiling: 2, unit: "clips/s" },
};

const LATENCY_CEILINGS: Record<string, Ceiling> = {
  inference_latency: { ceiling: 500, unit: "ms" },
  embedding_latency: { ceiling: 100, unit: "ms" },
};

const GAUGE_CEILINGS: Record<string, Ceiling> = {
  bulk_staged: { ceiling: 1000, unit: "staged" },
};

const LAG_CEILINGS: Record<string, Ceiling> = {
  decode: { ceiling: 100, unit: "msgs" },
  inference: { ceiling: 50, unit: "msgs" },
  bulk: { ceiling: 200, unit: "msgs" },
  bridge: { ceiling: 50, unit: "msgs" },
};

const ZONE_BG = {
  green: "rgba(34, 197, 94, 0.12)",
  yellow: "rgba(234, 179, 8, 0.14)",
  red: "rgba(239, 68, 68, 0.14)",
};

const LINE_COLOR = {
  green: "#16a34a",
  yellow: "#ca8a04",
  red: "#dc2626",
};

const FILL_COLOR = {
  green: "rgba(22, 163, 74, 0.25)",
  yellow: "rgba(202, 138, 4, 0.25)",
  red: "rgba(220, 38, 38, 0.25)",
};

function zoneFor(value: number): "green" | "yellow" | "red" {
  if (value < 50) return "green";
  if (value < 80) return "yellow";
  return "red";
}

// ----------------------------------------------------------------------
// MiniMonitor — Activity-Monitor-style mini graph
// ----------------------------------------------------------------------

interface MiniMonitorProps {
  title: string;
  subtitle?: string;
  data: number[]; // 0-100 normalized rolling buffer
  currentValue: string;
  width?: number;
  height?: number;
}

function MiniMonitor({
  title,
  subtitle,
  data,
  currentValue,
  width = 240,
  height = 80,
}: MiniMonitorProps) {
  const innerH = height;
  const innerW = width;

  // Right-align: pad with empty (0) values on the left until the buffer fills
  const padded = data.length >= HISTORY_SIZE
    ? data.slice(-HISTORY_SIZE)
    : [...Array(HISTORY_SIZE - data.length).fill(0), ...data];

  const stepX = innerW / Math.max(1, padded.length - 1);
  const yFor = (v: number) => innerH - (Math.min(100, Math.max(0, v)) / 100) * innerH;

  const linePoints = padded.map((v, i) => `${i * stepX},${yFor(v)}`).join(" ");
  const areaPoints = `0,${innerH} ${linePoints} ${innerW},${innerH}`;

  // Zone band Y-coordinates
  const greenTop = yFor(50);
  const yellowTop = yFor(80);

  const last = padded[padded.length - 1] ?? 0;
  const z = zoneFor(last);

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3 flex flex-col">
      <div className="flex items-baseline justify-between mb-1.5">
        <div className="text-xs font-medium text-gray-800 truncate">{title}</div>
      </div>
      <svg
        width={innerW}
        height={innerH}
        viewBox={`0 0 ${innerW} ${innerH}`}
        className="block"
        preserveAspectRatio="none"
      >
        {/* Zone backgrounds (red top, yellow middle, green bottom) */}
        <rect x={0} y={0} width={innerW} height={yellowTop} fill={ZONE_BG.red} />
        <rect
          x={0}
          y={yellowTop}
          width={innerW}
          height={greenTop - yellowTop}
          fill={ZONE_BG.yellow}
        />
        <rect
          x={0}
          y={greenTop}
          width={innerW}
          height={innerH - greenTop}
          fill={ZONE_BG.green}
        />
        {/* Filled area under the line */}
        <polygon points={areaPoints} fill={FILL_COLOR[z]} />
        {/* Line */}
        <polyline
          points={linePoints}
          fill="none"
          stroke={LINE_COLOR[z]}
          strokeWidth={1.25}
          strokeLinejoin="round"
        />
      </svg>
      <div className="mt-1.5 flex items-baseline justify-between">
        <span
          className="font-mono text-sm font-semibold"
          style={{ color: LINE_COLOR[z] }}
        >
          {currentValue}
        </span>
        {subtitle && (
          <span className="text-[10px] text-gray-500 truncate ml-2">
            {subtitle}
          </span>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------------
// Helpers — rate computation + label sum
// ----------------------------------------------------------------------

function sumByPrefix(
  service: ServiceMetrics | undefined,
  prefix: string,
): number {
  if (!service) return 0;
  let total = 0;
  for (const [k, v] of Object.entries(service)) {
    if (k === "error") continue;
    if (k === prefix || k.startsWith(prefix + "{")) {
      total += Number(v) || 0;
    }
  }
  return total;
}

function rateBetween(
  current: PollSample | null,
  previous: PollSample | null,
  serviceKey: string,
  metricPrefix: string,
): number {
  if (!current || !previous) return 0;
  const dt = current.ts - previous.ts;
  if (dt <= 0) return 0;
  const cur = sumByPrefix(current.services[serviceKey], metricPrefix);
  const prev = sumByPrefix(previous.services[serviceKey], metricPrefix);
  const dv = cur - prev;
  if (dv < 0) return 0; // counter reset
  return dv / dt;
}

function avgLatency(
  current: PollSample | null,
  previous: PollSample | null,
  serviceKey: string,
  sumPrefix: string,
  countPrefix: string,
): number {
  if (!current || !previous) return 0;
  const dSum =
    sumByPrefix(current.services[serviceKey], sumPrefix) -
    sumByPrefix(previous.services[serviceKey], sumPrefix);
  const dCount =
    sumByPrefix(current.services[serviceKey], countPrefix) -
    sumByPrefix(previous.services[serviceKey], countPrefix);
  if (dCount <= 0) return 0;
  return dSum / dCount;
}

function gaugeValue(
  current: PollSample | null,
  serviceKey: string,
  metricPrefix: string,
): number {
  if (!current) return 0;
  return sumByPrefix(current.services[serviceKey], metricPrefix);
}

function normalize(value: number, ceiling: number): number {
  if (ceiling <= 0) return 0;
  return Math.min(100, (value / ceiling) * 100);
}

// ----------------------------------------------------------------------
// Page
// ----------------------------------------------------------------------

interface GraphState {
  values: number[]; // normalized 0-100
  current: string; // formatted display value
  subtitle?: string;
}

const PROCESSING_GRAPHS = [
  "edge_agent_fps",
  "decode_service_fps",
  "inference_worker_fps",
  "inference_latency",
  "embedding_latency",
  "event_engine_eps",
  "clip_service_rate",
  "bulk_staged",
] as const;

const LAG_GRAPHS = [
  "decode_lag",
  "inference_lag",
  "bulk_lag",
  "bridge_lag",
] as const;

type GraphKey =
  | (typeof PROCESSING_GRAPHS)[number]
  | (typeof LAG_GRAPHS)[number];

export default function PipelineMonitorPage() {
  const role = getUserRole();
  const [error, setError] = useState<string | null>(null);
  const [lastFetch, setLastFetch] = useState<number>(Date.now());
  const [groups, setGroups] = useState<Record<string, KafkaGroup>>({});
  const [purgeBusy, setPurgeBusy] = useState<string | null>(null);
  const [purgeResult, setPurgeResult] = useState<
    { group: string; success: boolean; message: string } | null
  >(null);

  // Rolling per-graph buffers (kept in a ref so updates don't re-render the whole tree).
  const buffersRef = useRef<Record<string, GraphState>>({});
  const samplesRef = useRef<{ current: PollSample | null; previous: PollSample | null }>({
    current: null,
    previous: null,
  });
  const [, setTick] = useState(0);

  const pushBuffer = useCallback((key: string, normalized: number, current: string, subtitle?: string) => {
    const buf = buffersRef.current[key] ?? { values: [], current: "—" };
    buf.values = [...buf.values, normalized].slice(-HISTORY_SIZE);
    buf.current = current;
    buf.subtitle = subtitle;
    buffersRef.current[key] = buf;
  }, []);

  const updateGraphs = useCallback(() => {
    const cur = samplesRef.current.current;
    const prev = samplesRef.current.previous;
    if (!cur) return;

    // ---- Throughput rates ----
    const edgeFps = rateBetween(cur, prev, "edge_agent", "edge_motion_frames_total");
    pushBuffer(
      "edge_agent_fps",
      normalize(edgeFps, RATE_CEILINGS.edge_agent.ceiling),
      `${edgeFps.toFixed(1)} fps`,
    );

    const decodeFps = rateBetween(cur, prev, "decode_service", "decode_frames_decoded_total");
    pushBuffer(
      "decode_service_fps",
      normalize(decodeFps, RATE_CEILINGS.decode_service.ceiling),
      `${decodeFps.toFixed(1)} fps`,
    );

    const inferFps = rateBetween(cur, prev, "inference_worker", "inference_frames_consumed_total");
    pushBuffer(
      "inference_worker_fps",
      normalize(inferFps, RATE_CEILINGS.inference_worker.ceiling),
      `${inferFps.toFixed(1)} fps`,
    );

    // ---- Latencies (avg over last interval) ----
    const inferLat = avgLatency(
      cur,
      prev,
      "inference_worker",
      "inference_latency_ms_sum",
      "inference_latency_ms_count",
    );
    pushBuffer(
      "inference_latency",
      normalize(inferLat, LATENCY_CEILINGS.inference_latency.ceiling),
      `${inferLat.toFixed(0)} ms`,
    );

    const embLat = avgLatency(
      cur,
      prev,
      "inference_worker",
      "inference_embedding_latency_ms_sum",
      "inference_embedding_latency_ms_count",
    );
    pushBuffer(
      "embedding_latency",
      normalize(embLat, LATENCY_CEILINGS.embedding_latency.ceiling),
      `${embLat.toFixed(0)} ms`,
    );

    // ---- Downstream throughput ----
    const eventEps = rateBetween(cur, prev, "event_engine", "event_tracklets_consumed_total");
    pushBuffer(
      "event_engine_eps",
      normalize(eventEps, RATE_CEILINGS.event_engine.ceiling),
      `${eventEps.toFixed(1)} eps`,
    );

    const clipRate = rateBetween(cur, prev, "clip_service", "clip_extracted_total");
    pushBuffer(
      "clip_service_rate",
      normalize(clipRate, RATE_CEILINGS.clip_service.ceiling),
      `${clipRate.toFixed(2)} clips/s`,
    );

    // ---- Gauges ----
    const staged = gaugeValue(cur, "bulk_collector", "bulk_rows_staged");
    pushBuffer(
      "bulk_staged",
      normalize(staged, GAUGE_CEILINGS.bulk_staged.ceiling),
      `${Math.round(staged)} staged`,
    );

    // ---- Kafka consumer lag ----
    const decodeLag = gaugeValue(cur, "decode_service", "decode_consumer_lag");
    pushBuffer(
      "decode_lag",
      normalize(decodeLag, LAG_CEILINGS.decode.ceiling),
      `${Math.round(decodeLag)} msgs`,
    );

    const inferLag = gaugeValue(cur, "inference_worker", "inference_consumer_lag");
    pushBuffer(
      "inference_lag",
      normalize(inferLag, LAG_CEILINGS.inference.ceiling),
      `${Math.round(inferLag)} msgs`,
    );

    const bulkLag = gaugeValue(cur, "bulk_collector", "bulk_consumer_lag");
    pushBuffer(
      "bulk_lag",
      normalize(bulkLag, LAG_CEILINGS.bulk.ceiling),
      `${Math.round(bulkLag)} msgs`,
    );

    const bridgeLag = gaugeValue(cur, "ingress_bridge", "bridge_nats_consumer_lag");
    pushBuffer(
      "bridge_lag",
      normalize(bridgeLag, LAG_CEILINGS.bridge.ceiling),
      `${Math.round(bridgeLag)} msgs`,
    );

    setTick((t) => t + 1);
  }, [pushBuffer]);

  const fetchOnce = useCallback(async () => {
    try {
      const res = await fetch("/api/pipeline/metrics", { credentials: "include" });
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        return;
      }
      const data: MetricsResponse = await res.json();
      const sample: PollSample = { ts: data.ts, services: data.services };
      samplesRef.current.previous = samplesRef.current.current;
      samplesRef.current.current = sample;
      setError(null);
      setLastFetch(Date.now());
      updateGraphs();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fetch failed");
    }
  }, [updateGraphs]);

  // Load Kafka groups metadata once
  useEffect(() => {
    if (!isAdmin(role)) return;
    fetch("/api/pipeline/kafka/groups", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d?.groups && setGroups(d.groups))
      .catch(() => {});
  }, [role]);

  useEffect(() => {
    if (!isAdmin(role)) return;
    fetchOnce();
    const id = setInterval(fetchOnce, POLL_MS);
    return () => clearInterval(id);
  }, [role, fetchOnce]);

  // Tick once a second so "last polled Ns ago" stays current
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const purgeOne = async (group: string) => {
    const lagKey = lagKeyFor(group);
    const buf = lagKey ? buffersRef.current[lagKey] : undefined;
    const lagText = buf?.current ?? "an unknown number of messages";
    if (
      !confirm(
        `Purge ${groups[group]?.label ?? group}?\n\n` +
          `This will drop ${lagText} of unprocessed messages from ` +
          `consumer group "${group}". Cannot be undone.`,
      )
    ) {
      return;
    }
    setPurgeBusy(group);
    setPurgeResult(null);
    try {
      const res = await fetch(`/api/pipeline/kafka/purge/${group}`, {
        method: "POST",
        credentials: "include",
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        setPurgeResult({
          group,
          success: true,
          message: `Purged ${group}: ${body.output ?? "ok"}`.trim(),
        });
      } else {
        setPurgeResult({
          group,
          success: false,
          message: `Failed: ${body.detail ?? res.statusText}`,
        });
      }
    } catch (err) {
      setPurgeResult({
        group,
        success: false,
        message: err instanceof Error ? err.message : "purge failed",
      });
    } finally {
      setPurgeBusy(null);
    }
  };

  const purgeAll = async () => {
    if (
      !confirm(
        "Purge ALL Kafka queues?\n\n" +
          "This will drop unprocessed messages across the entire pipeline. " +
          "Are you absolutely sure?",
      )
    ) {
      return;
    }
    setPurgeBusy("__all__");
    setPurgeResult(null);
    try {
      const res = await fetch(`/api/pipeline/kafka/purge-all`, {
        method: "POST",
        credentials: "include",
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        const summary = Object.entries(body.purged ?? {})
          .map(([g, r]: [string, any]) => `${g}: ${r.success ? "ok" : "fail"}`)
          .join(", ");
        setPurgeResult({
          group: "all",
          success: !!body.all_succeeded,
          message: summary,
        });
      } else {
        setPurgeResult({
          group: "all",
          success: false,
          message: `Failed: ${body.detail ?? res.statusText}`,
        });
      }
    } finally {
      setPurgeBusy(null);
    }
  };

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  const secAgo = Math.floor((Date.now() - lastFetch) / 1000);
  const buffers = buffersRef.current;

  const monitor = (key: GraphKey, title: string, subtitle?: string) => {
    const b = buffers[key];
    return (
      <MiniMonitor
        key={key}
        title={title}
        subtitle={subtitle}
        data={b?.values ?? []}
        currentValue={b?.current ?? "—"}
      />
    );
  };

  // Map a Kafka consumer group → the buffer key holding its lag graph.
  // event-engine and clip-service don't publish a *_consumer_lag metric, so
  // we have no graph for them — return null and let the UI show a placeholder.
  function lagKeyFor(group: string): string | null {
    if (group === "decode-worker") return "decode_lag";
    if (group === "detector-worker") return "inference_lag";
    if (group === "bulk-collector-detections") return "bulk_lag";
    return null;
  }

  const lagForGroup = (group: string): number | null => {
    const key = lagKeyFor(group);
    if (!key) return null;
    return buffers[key]?.values.slice(-1)[0] ?? 0;
  };

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Pipeline Monitor</h1>
          <div className="text-xs text-gray-500 mt-0.5">
            5-minute rolling view · refreshes every 5 seconds
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

      <section className="bg-gray-50 border border-gray-200 rounded-lg p-4">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-600 mb-3">
          Processing pipeline
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {monitor("edge_agent_fps", "edge-agent", "frames captured")}
          {monitor("decode_service_fps", "decode-service", "frames decoded")}
          {monitor("inference_worker_fps", "inference-worker", "YOLOv8s · CPU")}
          {monitor("inference_latency", "inference latency", "per-frame avg")}
          {monitor("embedding_latency", "embedding latency", "Re-ID extractor")}
          {monitor("event_engine_eps", "event-engine", "tracklets/sec")}
          {monitor("clip_service_rate", "clip-service", "clips extracted")}
          {monitor("bulk_staged", "bulk-collector", "rows staged for DB")}
        </div>
      </section>

      <section className="bg-gray-50 border border-gray-200 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-600">
            Kafka queues
          </h2>
          <button
            type="button"
            onClick={purgeAll}
            disabled={purgeBusy !== null}
            className="text-xs text-red-700 border border-red-300 bg-red-50 hover:bg-red-100 disabled:opacity-50 rounded px-2 py-0.5"
          >
            {purgeBusy === "__all__" ? "Purging…" : "⚠ Purge All Queues"}
          </button>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {Object.entries(groups).map(([groupName, info]) => {
            const lagKey = lagKeyFor(groupName);
            const b = lagKey ? buffers[lagKey] : undefined;
            const lagPct = lagForGroup(groupName);
            // Disable purge only when we *know* there's nothing to drop.
            // For groups without a lag metric (lagPct===null), keep the button enabled.
            const empty = lagPct !== null && lagPct < 1;
            return (
              <div key={groupName} className="flex flex-col gap-1.5">
                {lagKey ? (
                  <MiniMonitor
                    title={info.label}
                    subtitle={groupName}
                    data={b?.values ?? []}
                    currentValue={b?.current ?? "—"}
                  />
                ) : (
                  <div className="bg-white border border-gray-200 rounded-lg p-3 flex flex-col">
                    <div className="text-xs font-medium text-gray-800 truncate mb-1.5">
                      {info.label}
                    </div>
                    <div
                      className="flex items-center justify-center text-[11px] text-gray-400"
                      style={{ height: 80 }}
                    >
                      lag metric not exported
                    </div>
                    <div className="mt-1.5 flex items-baseline justify-between">
                      <span className="font-mono text-sm text-gray-500">—</span>
                      <span className="text-[10px] text-gray-500 truncate ml-2">
                        {groupName}
                      </span>
                    </div>
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => purgeOne(groupName)}
                  disabled={purgeBusy !== null || empty}
                  className="text-[11px] text-orange-700 border border-orange-300 hover:bg-orange-50 disabled:opacity-40 disabled:cursor-not-allowed rounded px-2 py-0.5 self-start"
                >
                  {purgeBusy === groupName ? "Purging…" : "Purge"}
                </button>
              </div>
            );
          })}
        </div>
        {purgeResult && (
          <div
            className={`mt-3 text-xs p-2 rounded border ${
              purgeResult.success
                ? "bg-green-50 border-green-200 text-green-800"
                : "bg-red-50 border-red-200 text-red-800"
            }`}
          >
            <span className="font-mono">{purgeResult.group}</span> — {purgeResult.message}
          </div>
        )}
      </section>

      <div className="text-[11px] text-gray-400">
        Zone bands: <span className="text-green-700">green &lt; 50%</span> ·{" "}
        <span className="text-yellow-700">yellow 50-80%</span> ·{" "}
        <span className="text-red-700">red &gt; 80%</span> of capacity ceiling. Capacity
        ceilings are tuned per service for an i5-13500 CPU pilot deployment.
      </div>
    </div>
  );
}
