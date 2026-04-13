"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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

const REFRESH_MS = 60_000;

// Label, hours tuple. hours=0 means "everything".
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

function barColor(percent: number | null): string {
  if (percent === null) return "bg-gray-400";
  if (percent < 60) return "bg-green-500";
  if (percent < 80) return "bg-yellow-500";
  return "bg-red-500";
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
              {opt.label === "ALL objects"
                ? `Purge ${opt.label}`
                : `Purge ${opt.label}`}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function StoragePage() {
  const role = getUserRole();
  const [data, setData] = useState<BucketsResponse | null>(null);
  const [config, setConfig] = useState<StorageConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastFetch, setLastFetch] = useState<number>(Date.now());
  const [purgingBucket, setPurgingBucket] = useState<string | null>(null);
  const [lastPurge, setLastPurge] = useState<PurgeResult | null>(null);
  const [host, setHost] = useState<string>("localhost");

  useEffect(() => {
    if (typeof window !== "undefined") setHost(window.location.hostname);
  }, []);

  const loadBuckets = useCallback(async () => {
    try {
      const res = await fetch("/api/storage/buckets", { credentials: "include" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = (await res.json()) as BucketsResponse;
      setData(body);
      setError(null);
      setLastFetch(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fetch failed");
    }
  }, []);

  useEffect(() => {
    if (!isAdmin(role)) return;
    loadBuckets();
    fetch("/api/storage/config", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setConfig)
      .catch(() => {});
    const id = setInterval(loadBuckets, REFRESH_MS);
    return () => clearInterval(id);
  }, [role, loadBuckets]);

  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  async function onPurge(bucket: string, hours: number, label: string) {
    const confirmMsg =
      hours === 0
        ? `Delete EVERY object in bucket "${bucket}"? This cannot be undone.`
        : `Delete all objects in "${bucket}" ${label}? This cannot be undone.`;
    if (!window.confirm(confirmMsg)) return;

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
      await loadBuckets();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Purge failed");
    } finally {
      setPurgingBucket(null);
    }
  }

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  const secSinceFetch = Math.floor((Date.now() - lastFetch) / 1000);
  const percent = data?.usage_percent ?? null;
  const catalog = new Map<string, BucketCatalogEntry>(
    (config?.buckets ?? []).map((b) => [b.name, b]),
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Storage Management</h1>
        <div className="text-xs text-gray-500">
          {error ? (
            <span className="text-red-600">Error: {error}</span>
          ) : (
            <span>Last updated: {secSinceFetch}s ago · refresh every {REFRESH_MS / 1000}s</span>
          )}
        </div>
      </div>

      {/* Cluster usage bar */}
      {data && (
        <section className="bg-white border border-gray-200 rounded-lg p-4 space-y-2">
          <div className="flex items-center justify-between text-sm">
            <div>
              <span className="font-medium">{data.total_used_human}</span>
              <span className="text-gray-400"> used</span>
              {data.cluster_total_human && (
                <>
                  <span className="text-gray-400"> of </span>
                  <span className="font-medium">{data.cluster_total_human}</span>
                  <span className="text-gray-400"> cluster capacity</span>
                </>
              )}
            </div>
            {percent !== null && (
              <span className="font-mono text-sm">{percent.toFixed(1)}%</span>
            )}
          </div>
          <div className="w-full h-3 bg-gray-100 rounded">
            <div
              className={`h-full rounded transition-all ${barColor(percent)}`}
              style={{ width: `${Math.min(100, percent ?? 0)}%` }}
            />
          </div>
          {data.cluster_free_human && (
            <div className="text-xs text-gray-400">
              {data.cluster_free_human} free
            </div>
          )}
        </section>
      )}

      {/* Last purge result */}
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
                <th className="text-right px-3 py-2">Size</th>
                <th className="text-right px-3 py-2">Objects</th>
                <th className="text-left px-3 py-2">Purpose</th>
                <th className="text-left px-3 py-2">Retention</th>
                <th className="text-right px-3 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(data?.buckets ?? []).map((b) => {
                const cat = catalog.get(b.name);
                return (
                  <tr key={b.name} className="border-t border-gray-100">
                    <td className="px-3 py-2 font-mono">{b.name}</td>
                    <td className="px-3 py-2 text-right font-mono">{b.size_human}</td>
                    <td className="px-3 py-2 text-right font-mono">
                      {b.object_count.toLocaleString()}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-600">
                      {cat?.purpose ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-600">
                      {cat?.retention_days != null
                        ? `${cat.retention_days} days`
                        : "indefinite"}
                    </td>
                    <td className="px-3 py-2 text-right">
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
