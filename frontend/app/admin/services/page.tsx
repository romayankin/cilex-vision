"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const POLL_INTERVAL_MS = 15_000;

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

  // Tick once per second so countdowns update.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

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
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Services</h1>
          <p className="text-xs text-gray-500 mt-0.5">
            {services.length} container{services.length === 1 ? "" : "s"}
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
            {services.map((svc) => {
              const dot = statusDot(svc);
              const showDiag = svc.status !== "running" || svc.watchdog;
              const isRestarting = restarting.has(svc.name);
              const wd = svc.watchdog;
              return (
                <FragmentRow
                  key={svc.name}
                  svc={svc}
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
            {services.length === 0 && !error && (
              <tr>
                <td colSpan={6} className="text-center text-gray-400 py-8 text-sm">
                  No containers found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FragmentRow({
  svc, dot, wd, now, isRestarting,
  onRestart, onToggleLogs, onToggleDiag,
  expandedLogs, expandedDiag, logs, diagnostics, diagDot, refreshLogs,
}: {
  svc: Service;
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
        <td className="px-3 py-2 font-mono text-xs">
          <div className="flex items-center gap-2">
            <span
              className={`inline-block w-2 h-2 rounded-full ${dot.color} ${dot.pulse ? "animate-pulse" : ""}`}
            />
            <span>{svc.name}</span>
          </div>
          <div className="text-[10px] text-gray-400 mt-0.5 truncate max-w-xs" title={svc.image}>
            {svc.image}
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
