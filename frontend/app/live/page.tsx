"use client";

import { useEffect, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface StreamInfo {
  camera_id: string;
  name: string;
  status: string;
  has_rtsp: boolean;
}

interface StreamsResponse {
  streams: StreamInfo[];
}

const GO2RTC_PORT = 1984;

function getStreamUrls(cameraId: string) {
  const host = typeof window !== "undefined" ? window.location.hostname : "localhost";
  const base = `http://${host}:${GO2RTC_PORT}`;
  return {
    mse_url: `${base}/api/stream.mp4?src=${cameraId}`,
    snapshot_url: `${base}/api/frame.jpeg?src=${cameraId}`,
  };
}

function CameraCard({
  stream,
  admin,
  onRemove,
}: {
  stream: StreamInfo;
  admin: boolean;
  onRemove: (id: string) => void;
}) {
  const [fallback, setFallback] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const urls = getStreamUrls(stream.camera_id);

  useEffect(() => {
    if (!fallback) return;
    const id = setInterval(() => setRefreshKey((k) => k + 1), 5000);
    return () => clearInterval(id);
  }, [fallback]);

  const isOnline = stream.status === "online";

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-100">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${
              isOnline ? "bg-green-500" : "bg-gray-300"
            }`}
          />
          <span className="font-medium text-sm truncate">
            {stream.name || stream.camera_id}
          </span>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-xs text-gray-400">{stream.camera_id}</span>
          {admin && (
            <button
              onClick={() => onRemove(stream.camera_id)}
              className="text-xs text-gray-400 hover:text-red-600 px-1"
              title="Remove camera"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {!stream.has_rtsp ? (
        <div className="aspect-video bg-gray-900 flex items-center justify-center text-gray-400 text-sm">
          No RTSP configured
        </div>
      ) : fallback ? (
        <img
          src={`${urls.snapshot_url}&t=${refreshKey}`}
          alt={stream.name}
          className="w-full aspect-video bg-black object-cover"
        />
      ) : (
        <video
          src={urls.mse_url}
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

function AddCameraForm({
  onAdded,
  onCancel,
}: {
  onAdded: () => void;
  onCancel: () => void;
}) {
  const [cameraId, setCameraId] = useState("");
  const [name, setName] = useState("");
  const [rtspUrl, setRtspUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    if (!cameraId || !name || !rtspUrl) {
      setError("All fields are required");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch("/api/streams/cameras", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ camera_id: cameraId, name, rtsp_url: rtspUrl }),
      });
      if (!res.ok) {
        throw new Error(`${res.status} ${res.statusText}`);
      }
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add camera");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4 space-y-3">
      <h2 className="font-medium">Add Camera</h2>
      <div className="space-y-2">
        <div>
          <label className="block text-sm text-gray-600 mb-1">Camera ID</label>
          <input
            type="text"
            value={cameraId}
            onChange={(e) => setCameraId(e.target.value)}
            placeholder="cam-3"
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">Name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Front Entrance"
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="block text-sm text-gray-600 mb-1">RTSP URL</label>
          <input
            type="text"
            value={rtspUrl}
            onChange={(e) => setRtspUrl(e.target.value)}
            placeholder="rtsp://admin:pass@192.168.1.70:554/Streaming/Channels/101"
            className="w-full border border-gray-300 rounded px-3 py-1.5 text-sm font-mono"
          />
          <p className="text-xs text-gray-400 mt-1">
            HiWatch/Hikvision: rtsp://user:pass@IP:554/Streaming/Channels/101
          </p>
        </div>
      </div>
      {error && (
        <div className="text-sm text-red-600">{error}</div>
      )}
      <div className="flex gap-2">
        <button
          onClick={handleSubmit}
          disabled={submitting}
          className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {submitting ? "Adding..." : "Add"}
        </button>
        <button
          onClick={onCancel}
          disabled={submitting}
          className="px-4 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

export default function LivePage() {
  const [streams, setStreams] = useState<StreamInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const admin = isAdmin(getUserRole());

  async function loadStreams() {
    try {
      const res = await fetch("/api/streams", { credentials: "include" });
      if (!res.ok) {
        throw new Error(`${res.status} ${res.statusText}`);
      }
      const data: StreamsResponse = await res.json();
      setStreams(data.streams ?? []);
      setError(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to load streams";
      setError(msg.includes("401") ? "Login required" : msg);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadStreams();
    const id = setInterval(loadStreams, 30000);
    return () => clearInterval(id);
  }, []);

  async function handleRemove(cameraId: string) {
    if (!confirm(`Remove camera ${cameraId}?`)) return;
    try {
      const res = await fetch(`/api/streams/cameras/${cameraId}`, {
        method: "DELETE",
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(`${res.status} ${res.statusText}`);
      }
      await loadStreams();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to remove camera");
    }
  }

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading cameras...</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Live Cameras</h1>
        {admin && !showForm && (
          <button
            onClick={() => setShowForm(true)}
            className="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
          >
            + Add Camera
          </button>
        )}
      </div>

      {showForm && (
        <AddCameraForm
          onAdded={() => {
            setShowForm(false);
            loadStreams();
          }}
          onCancel={() => setShowForm(false)}
        />
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {streams.length === 0 && !error && (
        <div className="text-center py-12 text-gray-400">No cameras configured.</div>
      )}

      {streams.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {streams.map((s) => (
            <CameraCard
              key={s.camera_id}
              stream={s}
              admin={admin}
              onRemove={handleRemove}
            />
          ))}
        </div>
      )}
    </div>
  );
}
