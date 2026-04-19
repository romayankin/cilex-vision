"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Power, Wrench } from "lucide-react";
import { getToggles, setToggle, type ServiceToggle } from "@/lib/api-client";

const POLL_INTERVAL_MS = 15_000;
const ALERT_INTERVAL_MS = 15_000;
const ALERT_MUTE_STORAGE_KEY = "cilex-alert-muted";

interface ServiceMeta {
  description: string;
  priority: "P0" | "P1" | "P2" | "P3";
  priorityLabel: string;
}

// Static metadata. Update only when services are added/removed.
const SERVICE_CATALOG: Record<string, ServiceMeta> = {
  // P0 — system is useless without these
  timescaledb: {
    description:
      "PostgreSQL database with TimescaleDB extension. Stores all detections, tracks, events, audit logs, and system configuration. Every other service depends on it.",
    priority: "P0",
    priorityLabel: "If this stops, the entire system loses its memory — nothing works",
  },
  "kafka-0": {
    description:
      "Primary Kafka broker. Message bus connecting all pipeline stages — frames, detections, tracklets, events. If Kafka is down, no data flows through the system.",
    priority: "P0",
    priorityLabel: "If this stops, data stops flowing between all services — system is blind",
  },
  minio: {
    description:
      "S3-compatible object storage. Stores camera frames, decoded images, thumbnails, event clips, and debug traces. Pipeline cannot process frames without it.",
    priority: "P0",
    priorityLabel: "If this stops, no images or video can be stored or retrieved",
  },
  nats: {
    description:
      "Lightweight message broker for real-time frame delivery from cameras to the pipeline. Edge-agent publishes raw frames here.",
    priority: "P0",
    priorityLabel: "If this stops, cameras can't send video frames into the system",
  },
  "edge-agent": {
    description:
      "Connects to IP cameras via RTSP, captures video frames, and publishes them to NATS. One instance handles all cameras. No edge-agent = no video input.",
    priority: "P0",
    priorityLabel: "If this stops, all cameras are disconnected — no video comes in",
  },
  "decode-service": {
    description:
      "Receives raw frames from Kafka, decodes/resizes them for inference, and stores decoded images in MinIO. Sits between frame capture and AI detection.",
    priority: "P0",
    priorityLabel: "If this stops, raw video can't be prepared for AI analysis",
  },
  "inference-worker": {
    description:
      "Core AI engine. Runs YOLOv8s object detection, ByteTrack tracking, and OSNet Market-1501 Re-ID on every frame. Produces detections, tracks, and embeddings. (Thumbnail + debug-trace storage deprecated in phase 2.)",
    priority: "P0",
    priorityLabel: "If this stops, AI detection and tracking stops completely",
  },
  "query-api": {
    description:
      "FastAPI backend serving all REST endpoints — search, events, storage, admin, auth. The frontend and all API consumers depend on it. Also runs the service watchdog.",
    priority: "P0",
    priorityLabel: "If this stops, the website and all searches stop working",
  },
  frontend: {
    description:
      "Next.js web application. The operator-facing UI — live view, search, admin dashboards, this services page. If down, operators lose all visibility.",
    priority: "P0",
    priorityLabel: "If this stops, operators can't see anything — the screen goes blank",
  },

  // P1 — major feature broken
  "event-engine": {
    description:
      "Processes tracklets from Kafka and generates events: entered_scene, exited_scene, loitering. Without it, no security events are created from detections.",
    priority: "P1",
    priorityLabel: "If this stops, the system detects objects but won't create security alerts",
  },
  "clip-service": {
    description:
      "Stitches video frames into MP4 clips for each event. If down, events still generate but have no associated video clip for review.",
    priority: "P1",
    priorityLabel: "If this stops, events still appear but have no video clip attached",
  },
  "bulk-collector": {
    description:
      "Batches detections and tracks from Kafka and bulk-inserts them into TimescaleDB. If down, detections happen but aren't stored — search returns nothing new.",
    priority: "P1",
    priorityLabel: "If this stops, detections happen but aren't saved — search finds nothing new",
  },
  "ingress-bridge": {
    description:
      "Bridges frames from NATS to Kafka, applying sampling rules. Connects the real-time capture layer (NATS) to the processing pipeline (Kafka).",
    priority: "P1",
    priorityLabel: "If this stops, camera frames can't reach the AI pipeline for processing",
  },
  go2rtc: {
    description:
      "WebRTC/HLS/MSE streaming proxy. Provides live camera views in the browser and snapshot URLs. Detection pipeline works without it, but operators can't see live video.",
    priority: "P1",
    priorityLabel: "If this stops, live camera view in the browser goes dark",
  },

  // P2 — degraded but system runs
  ollama: {
    description:
      "Local AI model server (Ollama). Runs Gemma 2 2B for natural language search query parsing. Model loaded into RAM on demand, unloaded after 5 min idle.",
    priority: "P2",
    priorityLabel: "If this stops, AI search returns to manual filters only",
  },
  "attribute-service": {
    description:
      "Classifies track attributes (vehicle color, person clothing color). Enriches detections with metadata for filtering. Pipeline works without it but color filters are empty.",
    priority: "P2",
    priorityLabel: "If this stops, colour filters in search won't have any data",
  },
  "mtmc-service": {
    description:
      "Multi-target multi-camera tracking. Matches the same person/vehicle across different cameras using Re-ID embeddings. Cross-camera journeys unavailable if down.",
    priority: "P2",
    priorityLabel: "If this stops, the system can't match people across different cameras",
  },
  redis: {
    description:
      "In-memory cache used by mtmc-service for real-time embedding lookup. Only affects cross-camera matching performance.",
    priority: "P2",
    priorityLabel: "If this stops, cross-camera matching becomes slower",
  },
  prometheus: {
    description:
      "Metrics collection. Scrapes all services for performance data (latency, throughput, errors). Monitoring dashboards go blank if down, but pipeline is unaffected.",
    priority: "P2",
    priorityLabel: "If this stops, performance monitoring graphs go blank",
  },
  "kafka-1": {
    description:
      "Secondary Kafka broker. Provides replication and partition distribution. System works with only kafka-0 but has no redundancy — data loss risk on kafka-0 failure.",
    priority: "P2",
    priorityLabel: "If this stops, message system loses backup — higher risk if kafka-0 also fails",
  },
  "kafka-2": {
    description:
      "Tertiary Kafka broker. Same role as kafka-1 — adds redundancy and throughput capacity.",
    priority: "P2",
    priorityLabel: "If this stops, same as kafka-1 — less redundancy for messages",
  },

  // P3 — nice to have
  grafana: {
    description:
      "Dashboarding UI for Prometheus metrics. Pre-built panels for pipeline throughput and system health. Not essential — admin pages provide similar info.",
    priority: "P3",
    priorityLabel: "If this stops, Grafana dashboards unavailable but admin pages still work",
  },
  "kafka-ui": {
    description:
      "Web UI for inspecting Kafka topics, consumer groups, and message contents. Development/debugging tool only.",
    priority: "P3",
    priorityLabel: "If this stops, developers lose the Kafka inspection tool — no operator impact",
  },
  mlflow: {
    description:
      "ML experiment tracking and model registry. Used during model training and evaluation. Not needed for production inference.",
    priority: "P3",
    priorityLabel: "If this stops, ML experiment tracking unavailable — no operator impact",
  },
  "minio-init": {
    description:
      "One-shot initialization container. Creates MinIO buckets and sets lifecycle policies on first startup. Expected to exit after completing its job.",
    priority: "P3",
    priorityLabel: "Runs once at startup to create storage buckets, then exits — this is normal",
  },
  "ollama-init": {
    description:
      "One-shot container that pulls the Gemma 2 2B model on first startup (~1.5 GB download). Expected to exit after completing its job.",
    priority: "P3",
    priorityLabel: "Runs once to download AI model, then exits — this is normal",
  },
};

const UNKNOWN_SERVICE: ServiceMeta = {
  description: "No description available for this service.",
  priority: "P3",
  priorityLabel: "Unknown",
};

const PRIORITY_ORDER: Record<ServiceMeta["priority"], number> = {
  P0: 0, P1: 1, P2: 2, P3: 3,
};

const PRIORITY_COLORS: Record<ServiceMeta["priority"], string> = {
  P0: "bg-red-600 text-white",
  P1: "bg-orange-500 text-white",
  P2: "bg-yellow-500 text-gray-900",
  P3: "bg-gray-300 text-gray-700",
};

function metaFor(name: string): ServiceMeta {
  return SERVICE_CATALOG[name] ?? UNKNOWN_SERVICE;
}

// One-shot init containers exit cleanly by design — don't count them as down.
function isDown(svc: Service): boolean {
  if (svc.is_oneshot && svc.status === "exited" && svc.exit_code === 0) {
    return false;
  }
  return svc.status !== "running";
}

interface WatchdogState {
  attempt: number;
  max_attempts: number;
  last_attempt_at: string | null;
  next_retry_at: string | null;
  failed: boolean;
  diagnostics: Diagnostic[];
}

interface Service {
  name: string;
  status: string;
  health: string | null;
  image: string;
  started_at: string;
  uptime_seconds: number;
  exit_code: number | null;
  restart_count: number;
  is_oneshot?: boolean;
  watchdog?: WatchdogState;
}

interface Diagnostic {
  check: string;
  status: string;
  message: string;
  resolution: string;
}

function formatUptime(seconds: number): string {
  if (seconds <= 0) return "—";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400)
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

function formatCountdown(targetIso: string | null, now: number): string {
  if (!targetIso) return "—";
  const target = new Date(targetIso).getTime();
  const remainingS = Math.max(0, Math.floor((target - now) / 1000));
  if (remainingS === 0) return "now";
  if (remainingS < 60) return `${remainingS}s`;
  return `${Math.floor(remainingS / 60)}m ${remainingS % 60}s`;
}

function statusDot(svc: Service): { color: string; pulse: boolean } {
  if (svc.status !== "running") return { color: "bg-red-500", pulse: true };
  if (svc.health === "unhealthy") return { color: "bg-amber-500", pulse: true };
  if (svc.health === "starting") return { color: "bg-amber-500", pulse: true };
  if (svc.health === "healthy" || svc.health === null)
    return { color: "bg-green-500", pulse: false };
  return { color: "bg-gray-400", pulse: false };
}

export default function ServicesPage() {
  const [services, setServices] = useState<Service[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [expandedLogs, setExpandedLogs] = useState<string | null>(null);
  const [expandedDiag, setExpandedDiag] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, string>>({});
  const [diagnostics, setDiagnostics] = useState<Record<string, Diagnostic[]>>({});
  const [restarting, setRestarting] = useState<Set<string>>(new Set());
  const [toast, setToast] = useState<{ kind: "ok" | "err"; msg: string } | null>(null);
  const [now, setNow] = useState<number>(() => Date.now());
  const [alertMuted, setAlertMuted] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return sessionStorage.getItem(ALERT_MUTE_STORAGE_KEY) === "true";
  });
  const [audioBlocked, setAudioBlocked] = useState(false);
  const [toggles, setToggles] = useState<ServiceToggle[]>([]);
  const [togglesLoading, setTogglesLoading] = useState(true);
  const [togglesError, setTogglesError] = useState<string | null>(null);
  const [confirmToggle, setConfirmToggle] = useState<ServiceToggle | null>(null);
  const [togglePending, setTogglePending] = useState(false);

  // Pause auto-refresh while a logs/diagnostics panel is open so we don't
  // interrupt scroll position.
  const pausedRef = useRef(false);
  pausedRef.current = expandedLogs !== null || expandedDiag !== null;

  const fetchServices = useCallback(async () => {
    try {
      const res = await fetch("/api/admin/services", { credentials: "include" });
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        return;
      }
      const data = await res.json();
      setServices(data.services || []);
      setError(null);
      setLastUpdate(new Date());
    } catch (e) {
      setError(e instanceof Error ? e.message : "fetch failed");
    }
  }, []);

  useEffect(() => {
    fetchServices();
    const id = window.setInterval(() => {
      if (!pausedRef.current) fetchServices();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [fetchServices]);

  const loadToggles = useCallback(async () => {
    try {
      const { toggles } = await getToggles();
      setToggles(toggles);
      setTogglesError(null);
    } catch (err) {
      setTogglesError(err instanceof Error ? err.message : "Load failed");
    } finally {
      setTogglesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadToggles();
    const id = window.setInterval(loadToggles, 10_000);
    return () => window.clearInterval(id);
  }, [loadToggles]);

  const applyToggle = useCallback(
    async (t: ServiceToggle) => {
      setTogglePending(true);
      try {
        await setToggle(t.service_name, !t.enabled);
        await loadToggles();
        await fetchServices();
      } catch (err) {
        setToast({
          kind: "err",
          msg: `Toggle ${t.service_name}: ${err instanceof Error ? err.message : "failed"}`,
        });
      } finally {
        setTogglePending(false);
        setConfirmToggle(null);
      }
    },
    [loadToggles, fetchServices],
  );

  // Tick once per second so countdowns update.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const liveServices = useMemo(() => services.filter((s) => !s.is_oneshot), [services]);
  const oneshotServices = useMemo(() => services.filter((s) => s.is_oneshot), [services]);

  const sorted = useMemo(() => {
    return [...liveServices].sort((a, b) => {
      const pDiff = PRIORITY_ORDER[metaFor(a.name).priority] - PRIORITY_ORDER[metaFor(b.name).priority];
      if (pDiff !== 0) return pDiff;
      return a.name.localeCompare(b.name);
    });
  }, [liveServices]);

  const p0Down = useMemo(() => {
    return liveServices.filter((s) => metaFor(s.name).priority === "P0" && isDown(s));
  }, [liveServices]);

  const otherIssues = useMemo(() => {
    return liveServices.filter((s) => isDown(s) && metaFor(s.name).priority !== "P0").length;
  }, [liveServices]);

  // Persist mute preference for the session.
  useEffect(() => {
    sessionStorage.setItem(ALERT_MUTE_STORAGE_KEY, String(alertMuted));
  }, [alertMuted]);

  // Auto-unmute when all P0 services recover, so the next incident alerts again.
  useEffect(() => {
    if (p0Down.length === 0 && alertMuted) {
      setAlertMuted(false);
    }
  }, [p0Down.length, alertMuted]);

  // Audio alert: 3 short beeps every 15s while any P0 service is down.
  useEffect(() => {
    if (p0Down.length === 0 || alertMuted) {
      setAudioBlocked(false);
      return;
    }
    const Ctor: typeof AudioContext | undefined =
      window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;

    const ctx = new Ctor();
    let cancelled = false;

    function playBeepPattern() {
      if (cancelled || ctx.state === "closed") return;
      // Browsers block audio until the user has interacted with the page.
      if (ctx.state === "suspended") {
        setAudioBlocked(true);
        ctx.resume().catch(() => {});
        return;
      }
      setAudioBlocked(false);
      const start = ctx.currentTime;
      for (let i = 0; i < 3; i++) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.frequency.value = 880;
        osc.type = "square";
        gain.gain.value = 0.3;
        const t = start + i * 0.2;
        osc.start(t);
        osc.stop(t + 0.1);
      }
    }

    playBeepPattern();
    const id = window.setInterval(playBeepPattern, ALERT_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      ctx.close().catch(() => {});
    };
  }, [p0Down.length, alertMuted]);

  // Auto-dismiss toasts.
  useEffect(() => {
    if (!toast) return;
    const id = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(id);
  }, [toast]);

  async function handleRestart(name: string) {
    if (!window.confirm(`Restart ${name}?`)) return;
    setRestarting((s) => new Set(s).add(name));
    try {
      const res = await fetch(`/api/admin/services/${name}/restart`, {
        method: "POST",
        credentials: "include",
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) {
        setToast({ kind: "ok", msg: `${name}: ${body.message ?? "restarted"}` });
      } else {
        setToast({ kind: "err", msg: `${name}: ${body.detail ?? `HTTP ${res.status}`}` });
      }
      await fetchServices();
    } catch (e) {
      setToast({ kind: "err", msg: e instanceof Error ? e.message : "request failed" });
    } finally {
      setRestarting((s) => {
        const next = new Set(s);
        next.delete(name);
        return next;
      });
    }
  }

  async function toggleLogs(name: string) {
    if (expandedLogs === name) {
      setExpandedLogs(null);
      return;
    }
    setExpandedDiag(null);
    setExpandedLogs(name);
    if (!logs[name]) {
      try {
        const res = await fetch(`/api/admin/services/${name}/logs?tail=50`, {
          credentials: "include",
        });
        const body = await res.json();
        setLogs((prev) => ({ ...prev, [name]: body.logs ?? "(no logs)" }));
      } catch (e) {
        setLogs((prev) => ({
          ...prev,
          [name]: `Error loading logs: ${e instanceof Error ? e.message : "unknown"}`,
        }));
      }
    }
  }

  async function toggleDiagnostics(name: string) {
    if (expandedDiag === name) {
      setExpandedDiag(null);
      return;
    }
    setExpandedLogs(null);
    setExpandedDiag(name);
    if (!diagnostics[name]) {
      try {
        const res = await fetch(`/api/admin/services/${name}/diagnostics`, {
          credentials: "include",
        });
        const body = await res.json();
        setDiagnostics((prev) => ({ ...prev, [name]: body.diagnostics || [] }));
      } catch (e) {
        setDiagnostics((prev) => ({
          ...prev,
          [name]: [
            {
              check: "Diagnostics",
              status: "error",
              message: e instanceof Error ? e.message : "request failed",
              resolution: "",
            },
          ],
        }));
      }
    }
  }

  function refreshLogs(name: string) {
    setLogs((prev) => {
      const { [name]: _, ...rest } = prev;
      return rest;
    });
    void toggleLogs(name);
    setExpandedLogs(name);
  }

  function diagDot(status: string): string {
    switch (status) {
      case "ok": return "bg-green-500";
      case "warning": return "bg-amber-500";
      case "error": return "bg-red-500";
      default: return "bg-gray-400";
    }
  }

  return (
    <div className="space-y-4">
      <section className="bg-white border border-gray-200 rounded-lg p-4">
        <h2 className="text-lg font-semibold mb-1 flex items-center gap-2">
          <Power className="w-5 h-5 text-amber-600" />
          Optional Services
        </h2>
        <p className="text-xs text-gray-500 mb-4">
          Turn off services you don&apos;t currently need to free up RAM. Core
          services (Kafka, Postgres, etc.) cannot be disabled.
        </p>

        {togglesLoading && toggles.length === 0 && (
          <div className="text-sm text-gray-400">Loading…</div>
        )}
        {togglesError && (
          <div className="bg-red-50 border border-red-200 text-red-700 rounded p-2 text-sm mb-2">
            {togglesError}
          </div>
        )}

        <div className="space-y-2">
          {toggles.map((t) => {
            const isRunning = t.container_status === "running";
            const mismatch = (t.enabled && !isRunning) || (!t.enabled && isRunning);
            return (
              <div
                key={t.service_name}
                className={`flex items-start gap-3 border rounded-lg p-3 ${
                  mismatch ? "border-amber-300 bg-amber-50" : "border-gray-200 bg-white"
                }`}
              >
                <button
                  type="button"
                  onClick={() => setConfirmToggle(t)}
                  disabled={togglePending}
                  className={`relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition disabled:opacity-50 disabled:cursor-not-allowed ${
                    t.enabled ? "bg-green-500" : "bg-gray-300"
                  }`}
                  aria-label={t.enabled ? "Disable" : "Enable"}
                >
                  <span
                    className={`inline-block h-5 w-5 transform rounded-full bg-white transition ${
                      t.enabled ? "translate-x-5" : "translate-x-1"
                    }`}
                  />
                </button>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono text-sm font-medium">{t.service_name}</span>
                    <span
                      className={`text-xs px-1.5 py-0.5 rounded ${
                        isRunning
                          ? "bg-green-100 text-green-700"
                          : "bg-gray-100 text-gray-600"
                      }`}
                    >
                      {t.container_status || "unknown"}
                    </span>
                    {t.ram_savings_mb ? (
                      <span className="text-xs text-gray-500">
                        saves ~{(t.ram_savings_mb / 1024).toFixed(1)} GB
                      </span>
                    ) : null}
                    {mismatch && (
                      <span className="text-xs text-amber-700 flex items-center gap-1">
                        <AlertTriangle className="w-3 h-3" /> state sync in progress
                      </span>
                    )}
                  </div>
                  {t.description && (
                    <p className="text-xs text-gray-600 mt-1">{t.description}</p>
                  )}
                  {t.impact && (
                    <p className="text-xs text-gray-500 mt-1 italic">
                      Impact: {t.impact}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Services</h1>
          <p className="text-xs text-gray-500 mt-0.5">
            {liveServices.length} service{liveServices.length === 1 ? "" : "s"}
            {lastUpdate && ` · updated ${lastUpdate.toLocaleTimeString()}`}
            {pausedRef.current && " · auto-refresh paused"}
          </p>
        </div>
        <button
          type="button"
          onClick={fetchServices}
          className="text-xs px-3 py-1.5 border border-gray-300 rounded hover:bg-gray-50"
        >
          ↻ Refresh
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          Failed to load services: {error}
        </div>
      )}

      {toast && (
        <div
          className={`rounded-lg p-3 text-sm border ${
            toast.kind === "ok"
              ? "bg-green-50 border-green-200 text-green-800"
              : "bg-red-50 border-red-200 text-red-800"
          }`}
        >
          {toast.msg}
        </div>
      )}

      {p0Down.length > 0 && (
        <div className="bg-red-600 text-white rounded-lg p-4 flex items-center gap-3 animate-pulse">
          <div className="flex-shrink-0">
            <svg
              className="w-8 h-8"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
              <line x1="12" y1="9" x2="12" y2="13" />
              <line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
          </div>
          <div className="min-w-0">
            <div className="font-bold text-lg">
              SYSTEM ALERT — {p0Down.length} critical service{p0Down.length > 1 ? "s" : ""} down
            </div>
            <div className="text-sm text-red-100">
              {p0Down.map((s) => s.name).join(", ")} — system cannot operate normally.
              Contact support if you cannot resolve this.
            </div>
            {!alertMuted && audioBlocked && (
              <div className="text-xs text-red-100 mt-1 italic">
                Click anywhere on the page to enable audio alerts.
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={() => setAlertMuted((m) => !m)}
            className="ml-auto flex-shrink-0 text-red-50 hover:text-white text-sm border border-red-300 rounded px-3 py-1 bg-red-700/30 hover:bg-red-700/60"
            title={alertMuted ? "Unmute audio alert" : "Mute audio alert (visual alert stays)"}
          >
            {alertMuted ? "🔔 Unmute" : "🔇 Mute"}
          </button>
        </div>
      )}

      <div className="text-sm text-gray-600">
        {liveServices.length} service{liveServices.length === 1 ? "" : "s"}:
        <span className="text-green-600 font-medium ml-2">
          {liveServices.filter((s) => s.status === "running").length} running
        </span>
        {p0Down.length > 0 && (
          <span className="text-red-600 font-medium ml-2">
            {p0Down.length} P0 down
          </span>
        )}
        {otherIssues > 0 && (
          <span className="text-amber-600 font-medium ml-2">
            {otherIssues} other issue{otherIssues === 1 ? "" : "s"}
          </span>
        )}
      </div>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase tracking-wider text-gray-500">
            <tr>
              <th className="text-left px-3 py-2">Name</th>
              <th className="text-left px-3 py-2">Status</th>
              <th className="text-left px-3 py-2">Health</th>
              <th className="text-left px-3 py-2">Uptime</th>
              <th className="text-left px-3 py-2">Watchdog</th>
              <th className="text-right px-3 py-2">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {sorted.map((svc) => {
              const dot = statusDot(svc);
              const showDiag = svc.status !== "running" || svc.watchdog;
              const isRestarting = restarting.has(svc.name);
              const wd = svc.watchdog;
              const meta = metaFor(svc.name);
              return (
                <FragmentRow
                  key={svc.name}
                  svc={svc}
                  meta={meta}
                  dot={dot}
                  wd={wd}
                  now={now}
                  isRestarting={isRestarting}
                  onRestart={() => handleRestart(svc.name)}
                  onToggleLogs={() => toggleLogs(svc.name)}
                  onToggleDiag={showDiag ? () => toggleDiagnostics(svc.name) : null}
                  expandedLogs={expandedLogs === svc.name}
                  expandedDiag={expandedDiag === svc.name}
                  logs={logs[svc.name] ?? ""}
                  diagnostics={diagnostics[svc.name] ?? []}
                  diagDot={diagDot}
                  refreshLogs={() => refreshLogs(svc.name)}
                />
              );
            })}
            {liveServices.length === 0 && !error && (
              <tr>
                <td colSpan={6} className="text-center text-gray-400 py-8 text-sm">
                  No containers found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {oneshotServices.length > 0 && (
        <details className="mt-6 bg-white border border-gray-200 rounded-lg p-4">
          <summary className="cursor-pointer text-sm font-semibold text-gray-700 flex items-center gap-2">
            <Wrench className="w-4 h-4" />
            Setup Logs
            <span className="text-xs text-gray-400 font-normal">
              ({oneshotServices.length} one-shot container{oneshotServices.length === 1 ? "" : "s"})
            </span>
          </summary>
          <p className="text-xs text-gray-500 mt-2 mb-3">
            These containers run once on first startup (e.g., model download, bucket creation)
            and then exit. They&apos;re listed here for diagnostic purposes. An &quot;exited&quot;
            status with code 0 is normal and expected.
          </p>
          <div className="space-y-2">
            {oneshotServices.map((svc) => {
              const meta = metaFor(svc.name);
              const cleanExit = svc.status === "exited" && svc.exit_code === 0;
              return (
                <div
                  key={svc.name}
                  className="border border-gray-200 rounded p-2 text-xs flex items-center gap-3 flex-wrap"
                >
                  <span className="font-mono font-medium">{svc.name}</span>
                  <span
                    className={`px-1.5 py-0.5 rounded ${
                      cleanExit
                        ? "bg-gray-100 text-gray-600"
                        : svc.status === "running"
                          ? "bg-blue-100 text-blue-700"
                          : "bg-red-100 text-red-700"
                    }`}
                  >
                    {svc.status}
                    {svc.exit_code !== null && svc.status === "exited" && ` (exit ${svc.exit_code})`}
                  </span>
                  <span className="text-gray-500 min-w-0 flex-1">{meta.description}</span>
                  <button
                    type="button"
                    onClick={() => toggleLogs(svc.name)}
                    className="ml-auto text-blue-600 hover:underline"
                  >
                    {expandedLogs === svc.name ? "Hide logs" : "View logs"}
                  </button>
                </div>
              );
            })}
            {oneshotServices.map((svc) =>
              expandedLogs === svc.name ? (
                <div key={`${svc.name}-logs`} className="bg-gray-50 rounded p-2">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-semibold text-gray-600">
                      {svc.name} — logs (last 50 lines)
                    </span>
                    <button
                      type="button"
                      onClick={() => refreshLogs(svc.name)}
                      className="text-[10px] text-gray-500 hover:text-gray-900"
                    >
                      ↻ refresh
                    </button>
                  </div>
                  <pre className="bg-gray-900 text-gray-100 text-[11px] font-mono p-3 rounded max-h-80 overflow-auto whitespace-pre-wrap">
                    {logs[svc.name] || "Loading…"}
                  </pre>
                </div>
              ) : null,
            )}
          </div>
        </details>
      )}

      {confirmToggle && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-lg p-6 max-w-md w-full">
            <h3 className="font-semibold text-lg mb-2">
              {confirmToggle.enabled ? "Disable" : "Enable"} {confirmToggle.service_name}?
            </h3>
            {confirmToggle.enabled && confirmToggle.impact && (
              <div className="bg-amber-50 border border-amber-200 rounded p-3 text-sm text-amber-800 mb-4">
                <strong>Impact:</strong> {confirmToggle.impact}
              </div>
            )}
            {!confirmToggle.enabled && (
              <p className="text-sm text-gray-600 mb-4">
                This will start the <code>{confirmToggle.service_name}</code>{" "}
                container. It may take up to 30 seconds to become healthy.
              </p>
            )}
            <div className="flex gap-2 justify-end">
              <button
                type="button"
                onClick={() => setConfirmToggle(null)}
                disabled={togglePending}
                className="px-4 py-2 border border-gray-300 rounded text-sm disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => applyToggle(confirmToggle)}
                disabled={togglePending}
                className={`px-4 py-2 rounded text-sm text-white disabled:opacity-50 ${
                  confirmToggle.enabled
                    ? "bg-amber-600 hover:bg-amber-700"
                    : "bg-green-600 hover:bg-green-700"
                }`}
              >
                {togglePending
                  ? "Working…"
                  : confirmToggle.enabled
                  ? "Disable"
                  : "Enable"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function FragmentRow({
  svc, meta, dot, wd, now, isRestarting,
  onRestart, onToggleLogs, onToggleDiag,
  expandedLogs, expandedDiag, logs, diagnostics, diagDot, refreshLogs,
}: {
  svc: Service;
  meta: ServiceMeta;
  dot: { color: string; pulse: boolean };
  wd: WatchdogState | undefined;
  now: number;
  isRestarting: boolean;
  onRestart: () => void;
  onToggleLogs: () => void;
  onToggleDiag: (() => void) | null;
  expandedLogs: boolean;
  expandedDiag: boolean;
  logs: string;
  diagnostics: Diagnostic[];
  diagDot: (status: string) => string;
  refreshLogs: () => void;
}) {
  return (
    <>
      <tr className="hover:bg-gray-50">
        <td className="px-3 py-3">
          <div className="flex items-start gap-2">
            <span
              className={`inline-block w-2 h-2 rounded-full mt-1.5 flex-shrink-0 ${dot.color} ${dot.pulse ? "animate-pulse" : ""}`}
            />
            <span
              className={`inline-flex items-center justify-center px-1.5 py-0.5 rounded text-[10px] font-bold leading-none mt-1 flex-shrink-0 ${PRIORITY_COLORS[meta.priority]}`}
              title={meta.priorityLabel}
            >
              {meta.priority}
            </span>
            <div className="min-w-0">
              <div className="font-mono text-xs font-medium text-gray-900">{svc.name}</div>
              <div
                className="text-[11px] text-gray-500 max-w-lg mt-0.5"
                title={meta.description}
              >
                {meta.description}
              </div>
              <div
                className="text-[10px] text-gray-400 max-w-lg"
                title={svc.image}
              >
                {svc.image}
              </div>
            </div>
          </div>
        </td>
        <td className="px-3 py-2 text-xs">
          <span className={svc.status === "running" ? "text-green-700" : "text-red-700 font-semibold"}>
            {svc.status}
          </span>
          {svc.exit_code !== null && svc.status !== "running" && (
            <span className="text-gray-400 ml-1">(exit {svc.exit_code})</span>
          )}
        </td>
        <td className="px-3 py-2 text-xs text-gray-600">{svc.health ?? "—"}</td>
        <td className="px-3 py-2 text-xs font-mono text-gray-600">
          {formatUptime(svc.uptime_seconds)}
        </td>
        <td className="px-3 py-2 text-xs">
          {wd ? (
            wd.failed ? (
              <span className="text-red-700 font-semibold">
                Failed — {wd.attempt}/{wd.max_attempts} exhausted
              </span>
            ) : (
              <span className="text-amber-700">
                Attempt {wd.attempt}/{wd.max_attempts}
                {wd.next_retry_at && ` — retry in ${formatCountdown(wd.next_retry_at, now)}`}
              </span>
            )
          ) : (
            <span className="text-gray-300">—</span>
          )}
        </td>
        <td className="px-3 py-2 text-right">
          <div className="flex items-center justify-end gap-1">
            <button
              type="button"
              onClick={onRestart}
              disabled={isRestarting}
              className="text-xs px-2 py-1 border border-gray-300 rounded hover:bg-blue-50 hover:border-blue-300 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isRestarting ? "…" : "Restart"}
            </button>
            <button
              type="button"
              onClick={onToggleLogs}
              className={`text-xs px-2 py-1 border rounded ${
                expandedLogs ? "bg-gray-100 border-gray-400" : "border-gray-300 hover:bg-gray-50"
              }`}
            >
              Logs
            </button>
            {onToggleDiag && (
              <button
                type="button"
                onClick={onToggleDiag}
                className={`text-xs px-2 py-1 border rounded ${
                  expandedDiag ? "bg-gray-100 border-gray-400" : "border-gray-300 hover:bg-gray-50"
                }`}
              >
                Diag
              </button>
            )}
          </div>
        </td>
      </tr>
      {expandedLogs && (
        <tr>
          <td colSpan={6} className="px-3 py-2 bg-gray-50">
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs font-semibold text-gray-600">Logs (last 50 lines)</span>
              <button
                type="button"
                onClick={refreshLogs}
                className="text-[10px] text-gray-500 hover:text-gray-900"
              >
                ↻ refresh
              </button>
            </div>
            <pre className="bg-gray-900 text-gray-100 text-[11px] font-mono p-3 rounded max-h-80 overflow-auto whitespace-pre-wrap">
              {logs || "Loading…"}
            </pre>
          </td>
        </tr>
      )}
      {expandedDiag && (
        <tr>
          <td colSpan={6} className="px-3 py-2 bg-gray-50">
            <div className="text-xs font-semibold text-gray-600 mb-2">Diagnostics</div>
            {diagnostics.length === 0 ? (
              <div className="text-xs text-gray-400">Running checks…</div>
            ) : (
              <ul className="space-y-2">
                {diagnostics.map((d, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs">
                    <span
                      className={`inline-block w-2 h-2 rounded-full mt-1.5 flex-shrink-0 ${diagDot(d.status)}`}
                    />
                    <div className="min-w-0">
                      <div className="text-gray-800">
                        <span className="font-medium">{d.check}:</span> {d.message}
                      </div>
                      {d.resolution && (
                        <div className="text-gray-500 mt-0.5">{d.resolution}</div>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
