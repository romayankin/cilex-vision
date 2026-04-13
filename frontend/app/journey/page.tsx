"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getTracks, type TrackSummaryResponse } from "@/lib/api-client";

export default function JourneyIndexPage() {
  const [tracks, setTracks] = useState<TrackSummaryResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await getTracks({ limit: 10 });
        setTracks(res.tracks ?? []);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to load tracks";
        setError(msg.includes("401") ? "Login required" : msg);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading tracks...</div>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Cross-Camera Journeys</h1>

      <p className="text-sm text-gray-500">
        Search for an object, then click through to see its cross-camera journey.
        Recent tracks are shown below.
      </p>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {tracks.length === 0 && !error && (
        <div className="text-center py-12 text-gray-400">
          No tracks found. Start a search to generate detection data.
        </div>
      )}

      {tracks.length > 0 && (
        <div className="space-y-2">
          {tracks.map((tr) => (
            <Link
              key={tr.local_track_id}
              href={`/journey/${tr.local_track_id}`}
              className="block bg-white border border-gray-200 rounded-lg p-3 hover:border-blue-300 hover:shadow-sm transition"
            >
              <div className="flex items-center justify-between">
                <div>
                  <span className="text-sm font-medium">{tr.object_class}</span>
                  <span className="mx-2 text-gray-300">|</span>
                  <span className="text-sm text-gray-500">{tr.camera_id}</span>
                </div>
                <div className="flex items-center gap-2 text-xs text-gray-400">
                  <span
                    className={`px-1.5 py-0.5 rounded ${
                      tr.state === "active"
                        ? "bg-green-100 text-green-700"
                        : "bg-gray-100 text-gray-500"
                    }`}
                  >
                    {tr.state}
                  </span>
                  <span>{new Date(tr.start_time).toLocaleString()}</span>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
