"use client";

import { useEffect, useState } from "react";

interface StreamInfo {
  camera_id: string;
  name: string;
  status: string;
  has_rtsp: boolean;
  mse_url: string;
  webrtc_url: string;
  hls_url: string;
  snapshot_url: string;
}

interface StreamsResponse {
  streams: StreamInfo[];
}

function CameraCard({ stream }: { stream: StreamInfo }) {
  const [fallback, setFallback] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (!fallback) return;
    const id = setInterval(() => setRefreshKey((k) => k + 1), 5000);
    return () => clearInterval(id);
  }, [fallback]);

  const isOnline = stream.status === "online";

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${
              isOnline ? "bg-green-500" : "bg-gray-300"
            }`}
          />
          <span className="font-medium text-sm">{stream.name || stream.camera_id}</span>
        </div>
        <span className="text-xs text-gray-400">{stream.camera_id}</span>
      </div>

      {!stream.has_rtsp ? (
        <div className="aspect-video bg-gray-900 flex items-center justify-center text-gray-400 text-sm">
          No RTSP configured
        </div>
      ) : fallback ? (
        <img
          src={`${stream.snapshot_url}&t=${refreshKey}`}
          alt={stream.name}
          className="w-full aspect-video bg-black object-cover"
          onError={() => {
            /* keep trying */
          }}
        />
      ) : (
        <video
          src={stream.mse_url}
          autoPlay
          muted
          playsInline
          onError={() => setFallback(true)}
          className="w-full aspect-video bg-black"
        />
      )}
    </div>
  );
}

export default function LivePage() {
  const [streams, setStreams] = useState<StreamInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const res = await fetch("/api/streams", { credentials: "include" });
        if (!res.ok) {
          throw new Error(`${res.status} ${res.statusText}`);
        }
        const data: StreamsResponse = await res.json();
        if (!cancelled) {
          setStreams(data.streams ?? []);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : "Failed to load streams";
          setError(msg.includes("401") ? "Login required" : msg);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    const id = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading cameras...</div>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Live Cameras</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {streams.length === 0 && !error && (
        <div className="text-center py-12 text-gray-400">
          No cameras configured.
        </div>
      )}

      {streams.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {streams.map((s) => (
            <CameraCard key={s.camera_id} stream={s} />
          ))}
        </div>
      )}
    </div>
  );
}
