"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface BucketRow {
  name: string;
  size_bytes: number;
  size_human: string;
  object_count: number;
  created: string | null;
  purgeable: boolean;
}

interface BucketsResponse {
  buckets: BucketRow[];
  total_used_bytes: number;
  total_used_human: string;
  cluster_total_bytes: number;
  cluster_total_human: string | null;
  cluster_free_bytes: number;
  cluster_free_human: string | null;
  usage_percent: number | null;
}

interface BucketCatalogEntry {
  name: string;
  purpose: string;
  retention_days: number | null;
  planned?: boolean;
}

interface StorageConfig {
  endpoint: string;
  console_port: number;
  buckets: BucketCatalogEntry[];
  volume_name: string;
  volume_path: string;
  note: string;
}

interface PurgeResult {
  bucket: string;
  older_than_hours: number;
  deleted_objects: number;
  freed_bytes: number;
  freed_human: string;
  cutoff: string;
}

interface WatchdogStats {
  ready: boolean;
  quota_percent: number;
  message?: string;
  disk_total?: number;
  disk_used?: number;
  disk_free?: number;
  non_video_used?: number;
  assignable?: number;
  video_bytes?: number;
  quota_bytes?: number;
  over_quota?: boolean;
  bucket_sizes?: Record<string, number>;
  checked_at?: string;
  disk_total_human?: string;
  disk_used_human?: string;
  disk_free_human?: string;
  non_video_used_human?: string;
  assignable_human?: string;
  video_bytes_human?: string;
  quota_bytes_human?: string;
  bucket_sizes_human?: Record<string, string>;
  purging?: boolean;
  purge_deleted?: number;
  purge_freed?: number;
  purge_freed_human?: string;
  purge_target?: number;
  purge_target_human?: string;
  last_purge?: {
    deleted: number;
    freed_human: string;
    completed_at: string;
  } | null;
}

const REFRESH_MS = 30_000;
const REFRESH_MS_ACTIVE = 3_000;
const REFRESH_MS_PURGE = 2_000;
const QUOTA_DEBOUNCE_MS = 500;

const PURGE_OPTIONS: { label: string; hours: number }[] = [
  { label: "older than 1 hour", hours: 1 },
  { label: "older than 4 hours", hours: 4 },
  { label: "older than 8 hours", hours: 8 },
  { label: "older than 12 hours", hours: 12 },
  { label: "older than 24 hours", hours: 24 },
  { label: "older than 3 days", hours: 72 },
  { label: "older than 5 days", hours: 120 },
  { label: "older than 7 days", hours: 168 },
  { label: "older than 14 days", hours: 336 },
  { label: "older than 30 days", hours: 720 },
  { label: "ALL objects", hours: 0 },
];

function humanBytes(n: number): string {
  let size = Math.abs(n);
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (size < 1024) return `${size.toFixed(1)} ${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)} PB`;
}

function PurgeMenu({
  bucket,
  onPurge,
  busy,
}: {
  bucket: string;
  onPurge: (hours: number, label: string) => void;
  busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        disabled={busy}
        className="text-xs px-2 py-1 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
      >
        {busy ? "Purging…" : "Purge ▾"}
      </button>
      {open && !busy && (
        <div className="absolute right-0 mt-1 w-52 bg-white border border-gray-200 rounded shadow-lg z-10 text-xs">
          {PURGE_OPTIONS.map((opt) => (
            <button
              key={opt.hours}
              onClick={() => {
                setOpen(false);
                onPurge(opt.hours, opt.label);
              }}
              className={`block w-full text-left px-3 py-1.5 hover:bg-gray-50 ${
                opt.hours === 0 ? "text-red-600 font-medium border-t border-gray-100" : ""
              }`}
            >
              Purge {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function DiskBar({ stats }: { stats: WatchdogStats }) {
  if (!stats.ready || !stats.disk_total) return null;
  const total = stats.disk_total;
  const nonVideo = stats.non_video_used ?? 0;
  const buckets = stats.bucket_sizes ?? {};
  const frame = buckets["frame-blobs"] ?? 0;
  const decoded = buckets["decoded-frames"] ?? 0;
  const video = frame + decoded;
  const free = Math.max(0, total - nonVideo - video);

  const pct = (v: number) => (v / total) * 100;
  // Quota line position: nonVideo is system/other, then quota_bytes is the
  // video cap — so the dashed red line sits at nonVideo + quota_bytes.
  const quotaMarkerPct = stats.quota_bytes
    ? Math.min(100, pct(nonVideo + stats.quota_bytes))
    : null;

  const segments = [
    { label: "System / other", bytes: nonVideo, color: "bg-gray-400" },
    { label: "frame-blobs", bytes: frame, color: "bg-blue-500" },
    { label: "decoded-frames", bytes: decoded, color: "bg-cyan-400" },
    { label: "Free", bytes: free, color: "bg-gray-100" },
  ];

  return (
    <div className="space-y-2">
      <div className="relative w-full h-7 bg-gray-100 rounded overflow-hidden flex">
        {segments.map((seg) => (
          <div
            key={seg.label}
            className={`${seg.color} h-full flex items-center justify-center text-[10px] text-white font-medium overflow-hidden`}
            style={{ width: `${pct(seg.bytes)}%` }}
            title={`${seg.label}: ${humanBytes(seg.bytes)} (${pct(seg.bytes).toFixed(1)}%)`}
          >
            {pct(seg.bytes) > 6 && (
              <span className={seg.color === "bg-gray-100" ? "text-gray-500" : ""}>
                {pct(seg.bytes).toFixed(0)}%
              </span>
            )}
          </div>
        ))}
        {quotaMarkerPct !== null && (
          <div
            className="absolute top-0 h-full border-l-2 border-dashed border-red-500"
            style={{ left: `${quotaMarkerPct}%` }}
            title={`Video quota limit: ${stats.quota_bytes_human ?? ""}`}
          />
        )}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600">
        {segments.map((seg) => (
          <div key={seg.label} className="flex items-center gap-1.5">
            <span className={`inline-block w-3 h-3 rounded-sm ${seg.color} border border-gray-200`} />
            <span>
              {seg.label}: <span className="font-mono">{humanBytes(seg.bytes)}</span>
            </span>
          </div>
        ))}
        <div className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-0 border-t-2 border-dashed border-red-500" />
          <span>
            Quota:{" "}
            <span className="font-mono">
              {stats.quota_bytes_human ?? humanBytes(stats.quota_bytes ?? 0)}
            </span>
          </span>
        </div>
      </div>
    </div>
  );
}

function StatusIndicator({ stats }: { stats: WatchdogStats }) {
  if (!stats.ready || !stats.quota_bytes || !stats.video_bytes) {
    return (
      <div className="text-sm text-gray-500">
        🟢 Watchdog warming up {stats.message ? `— ${stats.message}` : ""}
      </div>
    );
  }
  const ratio = stats.video_bytes / stats.quota_bytes;
  if (stats.over_quota) {
    return (
      <div className="text-sm text-red-600 font-medium">
        🔴 Over quota — auto-cleanup purging oldest frame-blobs
      </div>
    );
  }
  if (ratio >= 0.9) {
    return (
      <div className="text-sm text-yellow-700 font-medium">
        🟡 Approaching quota ({(ratio * 100).toFixed(0)}% of limit used)
      </div>
    );
  }
  return (
    <div className="text-sm text-green-700">
      🟢 Within quota ({(ratio * 100).toFixed(0)}% of limit used)
    </div>
  );
}

export default function StoragePage() {
  const role = getUserRole();
  const [data, setData] = useState<BucketsResponse | null>(null);
  const [config, setConfig] = useState<StorageConfig | null>(null);
  const [stats, setStats] = useState<WatchdogStats | null>(null);
  const [quotaDraft, setQuotaDraft] = useState<number | null>(null);
  const [quotaSaving, setQuotaSaving] = useState(false);
  const [quotaMessage, setQuotaMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFetch, setLastFetch] = useState<number>(Date.now());
  const [purgingBucket, setPurgingBucket] = useState<string | null>(null);
  const [purgeInitialSize, setPurgeInitialSize] = useState<number | null>(null);
  const [purgeJustCompleted, setPurgeJustCompleted] = useState<string | null>(null);
  const [lastPurge, setLastPurge] = useState<PurgeResult | null>(null);
  const [host, setHost] = useState<string>("localhost");

  useEffect(() => {
    if (typeof window !== "undefined") setHost(window.location.hostname);
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [bucketsRes, statsRes] = await Promise.all([
        fetch("/api/storage/buckets", { credentials: "include" }),
        fetch("/api/storage", { credentials: "include" }),
      ]);
      if (!bucketsRes.ok) throw new Error(`buckets HTTP ${bucketsRes.status}`);
      if (!statsRes.ok) throw new Error(`storage HTTP ${statsRes.status}`);
      setData((await bucketsRes.json()) as BucketsResponse);
      const s = (await statsRes.json()) as WatchdogStats;
      setStats(s);
      setQuotaDraft((q) => (q === null ? s.quota_percent : q));
      setError(null);
      setLastFetch(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fetch failed");
    }
  }, []);

  useEffect(() => {
    if (!isAdmin(role)) return;
    fetch("/api/storage/config", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setConfig)
      .catch(() => {});
  }, [role]);

  const pollInterval = purgingBucket
    ? REFRESH_MS_PURGE
    : stats?.purging
    ? REFRESH_MS_ACTIVE
    : REFRESH_MS;
  useEffect(() => {
    if (!isAdmin(role)) return;
    loadAll();
    const id = setInterval(loadAll, pollInterval);
    return () => clearInterval(id);
  }, [role, loadAll, pollInterval]);

  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  // Debounced quota PUT
  const serverQuota = stats?.quota_percent ?? null;
  useEffect(() => {
    if (quotaDraft === null || serverQuota === null) return;
    if (quotaDraft === serverQuota) return;
    const handle = window.setTimeout(async () => {
      setQuotaSaving(true);
      try {
        const res = await fetch("/api/storage/quota", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ percent: quotaDraft }),
        });
        if (!res.ok) throw new Error(`quota HTTP ${res.status}`);
        // Compute new quota ceiling and compare to current video usage so
        // the user knows whether auto-cleanup is about to start.
        const assignable = stats?.assignable ?? 0;
        const newCap = (assignable * quotaDraft) / 100;
        const videoBytes = stats?.video_bytes ?? 0;
        if (videoBytes > newCap) {
          setQuotaMessage(
            `Quota set to ${quotaDraft}%. Auto-cleanup will start within 60 seconds.`,
          );
        } else {
          setQuotaMessage(`Quota set to ${quotaDraft}%.`);
        }
        await loadAll();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Quota update failed");
      } finally {
        setQuotaSaving(false);
      }
    }, QUOTA_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [quotaDraft, serverQuota, loadAll, stats?.assignable, stats?.video_bytes]);

  // Auto-dismiss quota message after 6 seconds.
  useEffect(() => {
    if (!quotaMessage) return;
    const id = window.setTimeout(() => setQuotaMessage(null), 6000);
    return () => window.clearTimeout(id);
  }, [quotaMessage]);

  async function onPurge(bucket: string, hours: number, label: string) {
    const confirmMsg =
      hours === 0
        ? `Delete EVERY object in bucket "${bucket}"? This cannot be undone.`
        : `Delete all objects in "${bucket}" ${label}? This cannot be undone.`;
    if (!window.confirm(confirmMsg)) return;

    const bucketData = data?.buckets?.find((x) => x.name === bucket);
    setPurgeInitialSize(bucketData?.size_bytes ?? null);
    setPurgingBucket(bucket);
    setLastPurge(null);
    try {
      const res = await fetch("/api/storage/purge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ bucket, older_than_hours: hours }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text}`);
      }
      const result: PurgeResult = await res.json();
      setLastPurge(result);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Purge failed");
    } finally {
      setPurgingBucket(null);
      setPurgeInitialSize(null);
      setPurgeJustCompleted(bucket);
      window.setTimeout(() => {
        setPurgeJustCompleted((cur) => (cur === bucket ? null : cur));
      }, 2000);
    }
  }

  const catalog = useMemo(
    () => new Map<string, BucketCatalogEntry>((config?.buckets ?? []).map((b) => [b.name, b])),
    [config],
  );

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  const secSinceFetch = Math.floor((Date.now() - lastFetch) / 1000);
  const quotaGB =
    stats?.assignable && quotaDraft
      ? (stats.assignable * quotaDraft) / 100 / 1024 ** 3
      : null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Storage Management</h1>
        <div className="text-xs text-gray-500">
          {error ? (
            <span className="text-red-600">Error: {error}</span>
          ) : (
            <span>
              Last updated: {secSinceFetch}s ago · refresh every {REFRESH_MS / 1000}s
            </span>
          )}
        </div>
      </div>

      {/* Disk usage bar + status */}
      {stats && (
        <section className="bg-white border border-gray-200 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="font-medium">Disk usage</h2>
            <StatusIndicator stats={stats} />
          </div>
          {stats.ready ? (
            <>
              <DiskBar stats={stats} />
              <div className="text-xs text-gray-500 flex flex-wrap gap-x-4">
                <span>
                  Total: <span className="font-mono">{stats.disk_total_human}</span>
                </span>
                <span>
                  Assignable: <span className="font-mono">{stats.assignable_human}</span>
                </span>
                <span>
                  Video used: <span className="font-mono">{stats.video_bytes_human}</span>
                </span>
                <span>
                  Free: <span className="font-mono">{stats.disk_free_human}</span>
                </span>
                {stats.checked_at && (
                  <span>
                    Checked:{" "}
                    <span className="font-mono">
                      {new Date(stats.checked_at).toLocaleTimeString()}
                    </span>
                  </span>
                )}
              </div>
            </>
          ) : (
            <div className="text-sm text-gray-500">
              {stats.message ?? "Watchdog warming up…"}
            </div>
          )}
        </section>
      )}

      {/* Quota slider */}
      {stats && stats.ready && quotaDraft !== null && (
        <section className="bg-white border border-gray-200 rounded-lg p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="font-medium">Video quota</h2>
            <div className="text-xs text-gray-500">
              {quotaSaving ? "Saving…" : "Video buckets are auto-purged above this limit"}
            </div>
          </div>
          <div className="flex items-center gap-4">
            <input
              type="range"
              min={10}
              max={90}
              step={1}
              value={quotaDraft}
              onChange={(e) => setQuotaDraft(Number(e.target.value))}
              className="flex-1"
            />
            <div className="w-48 text-right text-sm">
              <span className="font-mono font-medium">{quotaDraft}%</span>
              {quotaGB !== null && (
                <span className="text-gray-500">
                  {" "}
                  = <span className="font-mono">{quotaGB.toFixed(1)} GB</span>
                </span>
              )}
            </div>
          </div>
          <div className="flex justify-between text-[10px] text-gray-400 font-mono">
            <span>10%</span>
            <span>50%</span>
            <span>90%</span>
          </div>
        </section>
      )}

      {/* Quota change confirmation */}
      {quotaMessage && (
        <div className="bg-blue-50 border border-blue-200 text-blue-800 rounded p-3 text-sm">
          {quotaMessage}
        </div>
      )}

      {/* Live auto-cleanup progress */}
      {stats?.purging && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4 space-y-2">
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 border-2 border-amber-400 border-t-amber-600 rounded-full animate-spin" />
            <span className="text-sm font-medium text-amber-800">
              Auto-cleanup in progress…
            </span>
          </div>
          <div className="text-sm text-amber-700">
            Deleted{" "}
            <span className="font-mono">
              {(stats.purge_deleted ?? 0).toLocaleString()}
            </span>{" "}
            objects · Freed{" "}
            <span className="font-mono">{stats.purge_freed_human || "0 B"}</span>{" "}
            of{" "}
            <span className="font-mono">
              {stats.purge_target_human || "—"}
            </span>{" "}
            target
          </div>
          {(stats.purge_target ?? 0) > 0 && (
            <div className="w-full h-2 bg-amber-100 rounded">
              <div
                className="h-full bg-amber-500 rounded transition-all"
                style={{
                  width: `${Math.min(
                    100,
                    ((stats.purge_freed ?? 0) / (stats.purge_target ?? 1)) * 100,
                  )}%`,
                }}
              />
            </div>
          )}
        </div>
      )}

      {/* Last auto-cleanup summary (shown when not actively purging) */}
      {stats && !stats.purging && stats.last_purge && (
        <div className="text-xs text-gray-500 flex items-center gap-1">
          <span className="text-green-600">✓</span>
          Last auto-cleanup: deleted{" "}
          <span className="font-mono">
            {stats.last_purge.deleted.toLocaleString()}
          </span>{" "}
          objects ({stats.last_purge.freed_human} freed) at{" "}
          <span className="font-mono">
            {new Date(stats.last_purge.completed_at).toLocaleTimeString()}
          </span>
        </div>
      )}

      {/* Manual purge result */}
      {lastPurge && (
        <div className="bg-blue-50 border border-blue-200 text-blue-800 rounded p-3 text-sm">
          Purged <span className="font-mono">{lastPurge.deleted_objects}</span>{" "}
          objects from <span className="font-mono">{lastPurge.bucket}</span> (
          {lastPurge.freed_human} freed).
        </div>
      )}

      {/* Bucket table */}
      <section>
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-xs text-gray-500 uppercase">
              <tr>
                <th className="text-left px-3 py-2">Bucket</th>
                <th className="text-left px-3 py-2">Description</th>
                <th className="text-right px-3 py-2">Size</th>
                <th className="text-right px-3 py-2">Objects</th>
                <th className="text-left px-3 py-2">Retention</th>
                <th className="text-right px-3 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(data?.buckets ?? []).map((b) => {
                const cat = catalog.get(b.name);
                return (
                  <tr key={b.name} className="border-t border-gray-100">
                    <td className="px-3 py-2 font-mono align-top">{b.name}</td>
                    <td className="px-3 py-2 text-xs text-gray-600 max-w-md">
                      {cat?.purpose ?? "—"}
                      {cat?.planned && (
                        <span className="ml-1 italic text-gray-400">
                          (coming soon)
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right font-mono align-top relative">
                      {purgingBucket === b.name &&
                        purgeInitialSize !== null &&
                        purgeInitialSize > 0 && (
                          <div className="absolute bottom-0 left-0 right-0 h-[3px] bg-gray-100 overflow-hidden">
                            <div
                              className="h-full bg-blue-500 transition-all duration-1000 ease-linear"
                              style={{
                                width: `${Math.min(
                                  100,
                                  ((purgeInitialSize -
                                    (b.size_bytes ?? purgeInitialSize)) /
                                    purgeInitialSize) *
                                    100,
                                )}%`,
                              }}
                            />
                          </div>
                        )}
                      <span
                        className={
                          purgingBucket === b.name
                            ? "text-blue-600 transition-colors"
                            : purgeJustCompleted === b.name
                            ? "text-green-600 transition-colors"
                            : ""
                        }
                      >
                        {b.size_human}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right font-mono align-top">
                      {b.object_count.toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-600 align-top">
                      {cat?.retention_days != null
                        ? `${cat.retention_days} days`
                        : "indefinite"}
                    </td>
                    <td className="px-3 py-2 text-right align-top">
                      {b.purgeable ? (
                        <PurgeMenu
                          bucket={b.name}
                          busy={purgingBucket === b.name}
                          onPurge={(hours, label) => onPurge(b.name, hours, label)}
                        />
                      ) : (
                        <span className="text-xs text-gray-400">protected</span>
                      )}
                    </td>
                  </tr>
                );
              })}
              {!data && !error && (
                <tr>
                  <td colSpan={6} className="px-3 py-6 text-center text-gray-400 text-xs">
                    Loading…
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {/* Storage configuration info */}
      {config && (
        <section className="bg-gray-50 border border-gray-200 rounded-lg p-4 text-sm space-y-2">
          <h2 className="font-medium">Storage configuration</h2>
          <div className="text-xs text-gray-600 space-y-1 font-mono">
            <div>endpoint: {config.endpoint}</div>
            <div>
              volume: {config.volume_name} → {config.volume_path}
            </div>
          </div>
          <p className="text-xs text-gray-500">{config.note}</p>
          <a
            href={`http://${host}:${config.console_port}`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block text-xs text-blue-600 hover:underline"
          >
            Open MinIO Console →
          </a>
        </section>
      )}
    </div>
  );
}
