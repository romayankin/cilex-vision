"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface ThumbnailSettings {
  max_per_track: number;
  options: number[];
}

function ToggleSwitch({
  enabled,
  onChange,
  saving,
}: {
  enabled: boolean;
  onChange: (v: boolean) => void;
  saving: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!enabled)}
      disabled={saving}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
        enabled ? "bg-blue-600" : "bg-gray-300"
      } ${saving ? "opacity-50 cursor-not-allowed" : ""}`}
      aria-pressed={enabled}
    >
      <span
        className={`inline-block h-4 w-4 rounded-full bg-white transition-transform ${
          enabled ? "translate-x-6" : "translate-x-1"
        }`}
      />
    </button>
  );
}

export default function SettingsPage() {
  const role = getUserRole();
  const admin = isAdmin(role);

  const [settings, setSettings] = useState<ThumbnailSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const savedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [syncStatus, setSyncStatus] = useState<
    "idle" | "waiting" | "synced" | "timeout"
  >("idle");
  const syncTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const syncTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const syncDismiss = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [accessLogEnabled, setAccessLogEnabled] = useState(false);
  const [savingAccessLog, setSavingAccessLog] = useState(false);
  const [accessLogError, setAccessLogError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/settings/access-log", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((d) => setAccessLogEnabled(Boolean(d.enabled)))
      .catch(() => {});
  }, []);

  const toggleAccessLog = async (enabled: boolean) => {
    setSavingAccessLog(true);
    setAccessLogError(null);
    try {
      const res = await fetch("/api/settings/access-log", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAccessLogEnabled(Boolean(data.enabled));
    } catch (err) {
      setAccessLogError(
        err instanceof Error ? err.message : "Failed to save",
      );
    } finally {
      setSavingAccessLog(false);
    }
  };

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/settings/thumbnails", { credentials: "include" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as ThumbnailSettings;
      setSettings(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load settings");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    return () => {
      if (savedTimer.current) clearTimeout(savedTimer.current);
      if (syncTimer.current) clearInterval(syncTimer.current);
      if (syncTimeout.current) clearTimeout(syncTimeout.current);
      if (syncDismiss.current) clearTimeout(syncDismiss.current);
    };
  }, []);

  const clearSyncTimers = () => {
    if (syncTimer.current) {
      clearInterval(syncTimer.current);
      syncTimer.current = null;
    }
    if (syncTimeout.current) {
      clearTimeout(syncTimeout.current);
      syncTimeout.current = null;
    }
  };

  const startSyncPoll = (expected: number) => {
    clearSyncTimers();
    if (syncDismiss.current) {
      clearTimeout(syncDismiss.current);
      syncDismiss.current = null;
    }
    setSyncStatus("waiting");

    syncTimer.current = setInterval(async () => {
      try {
        const res = await fetch("/api/settings/thumbnails/status", {
          credentials: "include",
        });
        if (!res.ok) return;
        const data = (await res.json()) as {
          synced: boolean;
          worker_value: number | null;
          db_value: number | null;
        };
        if (data.synced && data.worker_value === expected) {
          clearSyncTimers();
          setSyncStatus("synced");
          syncDismiss.current = setTimeout(() => setSyncStatus("idle"), 5000);
        }
      } catch {
        // transient — keep polling
      }
    }, 2000);

    syncTimeout.current = setTimeout(() => {
      clearSyncTimers();
      setSyncStatus("timeout");
    }, 60000);
  };

  const handleSelect = async (n: number) => {
    if (!settings || n === settings.max_per_track || saving) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/settings/thumbnails", {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_per_track: n }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail ?? `HTTP ${res.status}`);
      }
      const data = await res.json();
      setSettings((prev) =>
        prev ? { ...prev, max_per_track: data.max_per_track } : prev,
      );
      setSavedAt(Date.now());
      if (savedTimer.current) clearTimeout(savedTimer.current);
      savedTimer.current = setTimeout(() => setSavedAt(null), 3000);
      startSyncPoll(data.max_per_track);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  if (!admin) {
    return (
      <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 rounded-lg p-4 text-sm">
        This page is admin-only.
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <h1 className="text-xl font-semibold">Settings</h1>

      {/* Thumbnail card */}
      <section className="bg-white border border-gray-200 rounded-lg p-5 space-y-4">
        <div>
          <h2 className="font-medium text-base">Detection Thumbnails</h2>
        </div>

        <div className="text-sm text-gray-700 space-y-3 leading-relaxed">
          <p>
            When the AI detects a person, vehicle, or animal in a camera feed,
            it saves a small cropped photo of what it found. These photos
            appear in Search results so you can quickly see what was detected
            without watching full video footage.
          </p>

          <p>
            <span className="font-medium">What is &ldquo;per sighting&rdquo;?</span>{" "}
            Each time a person or object appears and moves through a camera&rsquo;s
            field of view, the system tracks them as one continuous sighting.
            For example, a person walking past a camera over 30 seconds counts
            as one sighting. This setting controls how many photos are saved
            during each sighting.
          </p>

          <div>
            <p className="font-medium mb-1">Choosing the right number:</p>
            <ul className="list-disc pl-5 space-y-1 text-gray-600">
              <li>
                <span className="font-mono text-gray-900">1</span> — One photo
                per sighting. Minimal storage use. You&rsquo;ll see a single
                snapshot of each detection.
              </li>
              <li>
                <span className="font-mono text-gray-900">5–10</span> — A
                handful of photos showing different moments. Good balance of
                detail and storage.
              </li>
              <li>
                <span className="font-mono text-gray-900">20–50</span> —
                Detailed coverage of each sighting. Useful when you need to
                review exactly what happened.
              </li>
              <li>
                <span className="font-mono text-gray-900">100</span> — Maximum
                detail. Saves a photo roughly every second during a sighting.
                Uses the most storage.
              </li>
            </ul>
          </div>
        </div>

        <div className="pt-2">
          <div className="text-xs text-gray-500 uppercase tracking-wide mb-2">
            Photos per sighting
          </div>

          {loading ? (
            <div className="text-sm text-gray-400">Loading…</div>
          ) : error ? (
            <div className="bg-red-50 border border-red-200 text-red-700 rounded p-2 text-sm">
              {error}
            </div>
          ) : settings ? (
            <>
              <div className="flex flex-wrap gap-2">
                {settings.options.map((n) => {
                  const active = settings.max_per_track === n;
                  return (
                    <button
                      key={n}
                      type="button"
                      disabled={saving}
                      onClick={() => handleSelect(n)}
                      className={`px-4 py-2 rounded-full border text-sm font-mono transition ${
                        active
                          ? "bg-blue-600 text-white border-blue-600"
                          : "bg-white text-gray-700 border-gray-300 hover:border-blue-400"
                      } disabled:opacity-50 disabled:cursor-not-allowed`}
                    >
                      {n}
                    </button>
                  );
                })}
              </div>

              <div className="mt-3 text-sm text-gray-600 flex items-center gap-3">
                <span>
                  Currently:{" "}
                  <span className="font-mono text-gray-900">
                    {settings.max_per_track}
                  </span>{" "}
                  photos per sighting
                </span>
                {savedAt && (
                  <span className="text-green-600 text-xs">✓ Saved</span>
                )}
              </div>
            </>
          ) : null}

          <div className="mt-4 text-xs text-gray-500 bg-gray-50 border border-gray-200 rounded p-2">
            ℹ Changes take effect within 30 seconds — the detection service
            polls for updates live.
          </div>

          {syncStatus === "waiting" && (
            <div className="mt-3 flex items-center gap-2 text-sm text-blue-700 bg-blue-50 border border-blue-200 rounded p-3">
              <svg
                className="animate-spin h-4 w-4"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                  fill="none"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                />
              </svg>
              Applying to detection service…
            </div>
          )}

          {syncStatus === "synced" && (
            <div className="mt-3 flex items-center gap-2 text-sm text-green-700 bg-green-50 border border-green-200 rounded p-3">
              <span aria-hidden="true">✓</span>
              Detection service updated successfully
            </div>
          )}

          {syncStatus === "timeout" && (
            <div className="mt-3 flex items-start gap-2 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded p-3">
              <span aria-hidden="true">⚠</span>
              <div>
                <p className="font-medium">
                  Detection service did not confirm the change within 60
                  seconds.
                </p>
                <p className="text-xs mt-1">
                  The service may need a manual restart:
                  <code className="bg-amber-100 px-1 rounded ml-1">
                    docker compose restart inference-worker
                  </code>
                </p>
              </div>
            </div>
          )}
        </div>
      </section>

      {/* Access log toggle */}
      <section className="bg-white border border-gray-200 rounded-lg p-5 space-y-4">
        <div className="flex items-center justify-between gap-4">
          <h2 className="font-semibold text-base">Access Log</h2>
          <div className="flex items-center gap-3">
            <span className="text-xs text-gray-500 uppercase tracking-wide">
              {accessLogEnabled ? "Enabled" : "Disabled"}
            </span>
            <ToggleSwitch
              enabled={accessLogEnabled}
              onChange={toggleAccessLog}
              saving={savingAccessLog}
            />
          </div>
        </div>

        {accessLogError && (
          <div className="bg-red-50 border border-red-200 text-red-700 rounded p-2 text-sm">
            {accessLogError}
          </div>
        )}

        <div className="text-sm text-gray-600 space-y-3 leading-relaxed">
          <p>
            <strong>What it does:</strong> When enabled, every API read request
            is recorded in a separate database table — who viewed which data,
            when, and from which IP address. This creates a complete trail of
            &ldquo;User X viewed detections from Camera 2 at 14:32 on April
            14th&rdquo; entries.
          </p>

          <p>
            <strong>Who needs it:</strong> This is a compliance feature.
            Organisations subject to data protection regulations (GDPR, 152-FZ,
            PCI DSS) may need to prove who accessed surveillance footage and
            when. If you don&rsquo;t have a compliance requirement, you
            probably don&rsquo;t need this.
          </p>

          <p>
            <strong>Impact on storage:</strong> Each logged request adds ~200
            bytes to the database. With the current 2 cameras, normal usage
            generates roughly 2,000–5,000 entries per day (~1 MB/day). The
            data is automatically deleted after 90 days by a TimescaleDB
            retention policy, so maximum storage is roughly 90 MB.
          </p>

          <div className="bg-gray-50 border border-gray-200 rounded p-3">
            <p className="font-medium text-gray-700 text-xs uppercase tracking-wide mb-2">
              Scaling estimate per additional camera
            </p>
            <table className="w-full text-xs">
              <tbody>
                <tr className="border-b border-gray-200">
                  <td className="py-1 text-gray-500">Additional DB writes</td>
                  <td className="py-1 text-right font-mono">
                    ~500–1,000/day per camera
                  </td>
                </tr>
                <tr className="border-b border-gray-200">
                  <td className="py-1 text-gray-500">
                    Additional storage (90-day window)
                  </td>
                  <td className="py-1 text-right font-mono">
                    ~15–30 MB per camera
                  </td>
                </tr>
                <tr className="border-b border-gray-200">
                  <td className="py-1 text-gray-500">At 8 cameras (90 days)</td>
                  <td className="py-1 text-right font-mono">~200 MB total</td>
                </tr>
                <tr className="border-b border-gray-200">
                  <td className="py-1 text-gray-500">At 32 cameras (90 days)</td>
                  <td className="py-1 text-right font-mono">~800 MB total</td>
                </tr>
                <tr>
                  <td className="py-1 text-gray-500">At 128 cameras (90 days)</td>
                  <td className="py-1 text-right font-mono">~3 GB total</td>
                </tr>
              </tbody>
            </table>
          </div>

          <p>
            <strong>Impact on detection processing:</strong> None. The access
            log only records API reads (search queries, page views, dashboard
            refreshes). It does not affect the camera pipeline, object
            detection, tracking, or any inference workload. Frame processing
            speed, detection latency, and thumbnail generation are completely
            unaffected regardless of whether this is on or off.
          </p>

          <p>
            <strong>Impact on API response time:</strong> Minimal. Each logged
            request adds one asynchronous database INSERT (~0.5ms). The write
            is fire-and-forget — it never blocks the API response. In
            practice, you won&rsquo;t notice any difference.
          </p>

          <p className="text-gray-500 text-xs">
            Admin actions (purges, settings changes, quota adjustments) are
            always logged in the Audit Log regardless of this setting. This
            toggle only controls the high-volume read-access tracking.
            Changes take effect within 30 seconds (middleware cache TTL).
          </p>
        </div>
      </section>

      {/* Edge agent read-only */}
      <section className="bg-white border border-gray-200 rounded-lg p-5 space-y-3">
        <h2 className="font-medium text-base">Edge Agent (Camera Frame Capture)</h2>
        <p className="text-sm text-gray-700 leading-relaxed">
          Controls how many frames per second each camera sends for analysis.
          Higher = more detections but more disk usage. Lower = fewer detections
          but saves storage.
        </p>
        <dl className="text-sm grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
          <dt className="text-gray-500">Max FPS</dt>
          <dd className="font-mono text-gray-900">2</dd>
          <dt className="text-gray-500">Motion threshold</dt>
          <dd className="font-mono text-gray-900">0.01</dd>
        </dl>
        <div className="text-xs text-gray-500 bg-gray-50 border border-gray-200 rounded p-2">
          ℹ Configured in the edge agent settings file. Requires service
          restart to change.
        </div>
      </section>
    </div>
  );
}
