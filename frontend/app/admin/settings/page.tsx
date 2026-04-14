"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface ThumbnailSettings {
  max_per_track: number;
  options: number[];
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
    };
  }, []);

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
            ℹ Changes take effect when the detection service restarts (usually
            within a few minutes).
          </div>
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
