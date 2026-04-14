"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { getUserRole, isAdmin } from "@/lib/auth";
import { getStreamUrls } from "@/lib/stream-urls";

interface StreamInfo {
  camera_id: string;
  name: string;
  status: string;
  has_rtsp: boolean;
}

interface StreamsResponse {
  streams: StreamInfo[];
}

interface DiscoveredCamera {
  ip: string;
  model: string | null;
  serial: string | null;
  firmware: string | null;
  mac: string | null;
  device_name: string | null;
  manufacturer: string | null;
  rtsp_url: string;
  already_added: boolean;
  existing_camera_id: string | null;
}

interface DiscoveryResponse {
  cameras: DiscoveredCamera[];
  scan_time_ms: number;
}

type SortKey = "name" | "id" | "status";
type ViewMode = "grid" | "single";
type StatusFilter = "all" | "online" | "offline";

function slugifyCameraId(model: string | null, ip: string): string {
  const base = (model || ip).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return base || `cam-${ip.split(".").pop()}`;
}

type PlayerMode = "mse" | "hls" | "snapshot";
type StreamQuality = "hd" | "fast";

function CameraPlayer({ stream, large = false }: { stream: StreamInfo; large?: boolean }) {
  const [mode, setMode] = useState<PlayerMode>("mse");
  const [quality, setQuality] = useState<StreamQuality>("hd");
  const [refreshKey, setRefreshKey] = useState(0);
  const [reconnectKey, setReconnectKey] = useState(0);
  const [fallbackNotice, setFallbackNotice] = useState<string | null>(null);
  const urls = getStreamUrls(stream.camera_id);

  useEffect(() => {
    if (mode !== "snapshot") return;
    const id = setInterval(() => setRefreshKey((k) => k + 1), 5000);
    return () => clearInterval(id);
  }, [mode]);

  useEffect(() => {
    if (!fallbackNotice) return;
    const t = setTimeout(() => setFallbackNotice(null), 3000);
    return () => clearTimeout(t);
  }, [fallbackNotice]);

  const selectQuality = (q: StreamQuality) => {
    setFallbackNotice(null);
    setQuality(q);
    setMode("mse");
    setReconnectKey((k) => k + 1);
  };

  const reconnect = () => {
    setMode("mse");
    setReconnectKey((k) => k + 1);
  };

  const handleError = () => {
    if (mode === "mse") {
      setMode("hls");
      return;
    }
    // HLS also failed. If we were on the sub-stream, fall back to HD before
    // giving up to a snapshot.
    if (quality === "fast") {
      setFallbackNotice("Sub-stream unavailable, using HD");
      setQuality("hd");
      setMode("mse");
      setReconnectKey((k) => k + 1);
      return;
    }
    setMode("snapshot");
  };

  if (!stream.has_rtsp) {
    return (
      <div className="aspect-video bg-gray-900 flex items-center justify-center text-gray-400 text-sm">
        No RTSP configured
      </div>
    );
  }

  const videoSrc =
    mode === "mse"
      ? quality === "hd" ? urls.mse_url : urls.mse_sub_url
      : quality === "hd" ? urls.hls_url : urls.hls_sub_url;
  const snapshotSrc = quality === "hd" ? urls.snapshot_url : urls.snapshot_sub_url;

  return (
    <>
      {mode === "snapshot" ? (
        <div className="relative">
          <img
            src={`${snapshotSrc}&t=${refreshKey}`}
            alt={stream.name}
            className="w-full aspect-video bg-black object-cover"
          />
          <button
            onClick={reconnect}
            className="absolute bottom-2 right-2 text-xs bg-black/60 text-white px-2 py-1 rounded hover:bg-black/80"
            title="Reconnect live stream"
          >
            ↻ Reconnect
          </button>
        </div>
      ) : (
        <video
          key={`${mode}-${quality}-${reconnectKey}`}
          src={videoSrc}
          autoPlay
          muted
          playsInline
          onError={handleError}
          className="w-full aspect-video bg-black object-cover"
        />
      )}
      <QualityBar
        quality={quality}
        onChange={selectQuality}
        notice={fallbackNotice}
        large={large}
      />
    </>
  );
}

function QualityBar({
  quality,
  onChange,
  notice,
  large,
}: {
  quality: StreamQuality;
  onChange: (q: StreamQuality) => void;
  notice: string | null;
  large?: boolean;
}) {
  const tone = large ? "text-gray-300" : "text-gray-400";
  return (
    <div
      className={`flex items-center gap-1 px-3 py-1.5 ${
        large ? "bg-black/60" : "border-t border-gray-100"
      }`}
    >
      <span className={`text-[10px] mr-1 ${tone}`}>Quality:</span>
      <button
        type="button"
        onClick={() => onChange("hd")}
        className={`px-2 py-0.5 text-[11px] rounded transition ${
          quality === "hd"
            ? "bg-blue-600 text-white"
            : "bg-gray-100 text-gray-600 hover:bg-gray-200"
        }`}
      >
        HD
      </button>
      <button
        type="button"
        onClick={() => onChange("fast")}
        className={`px-2 py-0.5 text-[11px] rounded transition ${
          quality === "fast"
            ? "bg-blue-600 text-white"
            : "bg-gray-100 text-gray-600 hover:bg-gray-200"
        }`}
      >
        Fast
      </button>
      <span className={`text-[10px] ml-auto ${tone}`}>
        {notice ??
          (quality === "hd" ? "1080p · 2-3s delay" : "Low-res · <1s delay")}
      </span>
    </div>
  );
}

function CameraCard({
  stream,
  admin,
  onRemove,
  onExpand,
}: {
  stream: StreamInfo;
  admin: boolean;
  onRemove: (id: string) => void;
  onExpand: (id: string) => void;
}) {
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
          <button
            onClick={() => onExpand(stream.camera_id)}
            className="text-xs text-gray-400 hover:text-blue-600 px-1"
            title="Expand"
          >
            ⤢
          </button>
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
      <CameraPlayer stream={stream} />
      <div className="flex items-center justify-between px-3 py-1.5 text-xs text-gray-500 border-t border-gray-100">
        <span className={isOnline ? "text-green-700" : "text-gray-400"}>
          {stream.status}
        </span>
        <Link
          href={`/timeline?camera_id=${stream.camera_id}`}
          className="hover:text-blue-600"
        >
          Timeline →
        </Link>
      </div>
    </div>
  );
}

function FullscreenView({
  stream,
  onClose,
}: {
  stream: StreamInfo;
  onClose: () => void;
}) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div className="fixed inset-0 bg-black/90 z-50 flex flex-col p-4">
      <div className="flex items-center justify-between text-white mb-3">
        <div>
          <h2 className="text-lg font-medium">{stream.name || stream.camera_id}</h2>
          <p className="text-sm text-gray-400">
            {stream.camera_id} · {stream.status}
          </p>
        </div>
        <button
          onClick={onClose}
          className="px-3 py-1.5 border border-gray-500 rounded text-sm hover:bg-white/10"
        >
          Close (Esc)
        </button>
      </div>
      <div className="flex-1 flex items-center justify-center">
        <div className="w-full max-w-6xl">
          <CameraPlayer stream={stream} large />
        </div>
      </div>
    </div>
  );
}

function SingleView({
  streams,
  activeId,
  onChange,
}: {
  streams: StreamInfo[];
  activeId: string | null;
  onChange: (id: string) => void;
}) {
  const active = streams.find((s) => s.camera_id === activeId) ?? streams[0];
  if (!active) return null;
  const idx = streams.indexOf(active);
  const prev = () => onChange(streams[(idx - 1 + streams.length) % streams.length].camera_id);
  const next = () => onChange(streams[(idx + 1) % streams.length].camera_id);

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-gray-100">
        <div className="flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${
              active.status === "online" ? "bg-green-500" : "bg-gray-300"
            }`}
          />
          <span className="font-medium">{active.name || active.camera_id}</span>
          <span className="text-xs text-gray-400">{active.camera_id}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={prev}
            className="px-2 py-1 border border-gray-300 rounded text-sm hover:bg-gray-50"
          >
            ← Prev
          </button>
          <span className="text-xs text-gray-500">
            {idx + 1} / {streams.length}
          </span>
          <button
            onClick={next}
            className="px-2 py-1 border border-gray-300 rounded text-sm hover:bg-gray-50"
          >
            Next →
          </button>
        </div>
      </div>
      <CameraPlayer stream={active} large />
    </div>
  );
}

function AddCameraForm({
  initial,
  onAdded,
  onCancel,
}: {
  initial?: { camera_id?: string; name?: string; rtsp_url?: string };
  onAdded: () => void;
  onCancel: () => void;
}) {
  const [cameraId, setCameraId] = useState(initial?.camera_id ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [rtspUrl, setRtspUrl] = useState(initial?.rtsp_url ?? "");
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
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
            Main stream URL (channel 101). The system automatically registers
            channel 102 as the low-latency sub-stream for the HD/Fast toggle.
          </p>
        </div>
      </div>
      {error && <div className="text-sm text-red-600">{error}</div>}
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

function DiscoveredCard({
  cam,
  onAdd,
}: {
  cam: DiscoveredCamera;
  onAdd: (cam: DiscoveredCamera) => void;
}) {
  const label = cam.model || cam.manufacturer || "Unknown camera";
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-3 text-sm space-y-1">
      <div className="flex items-center justify-between">
        <span className="font-medium">{label}</span>
        {cam.already_added ? (
          <span className="text-xs text-green-700 bg-green-50 px-2 py-0.5 rounded">
            ✓ Added as {cam.existing_camera_id}
          </span>
        ) : (
          <span className="text-xs text-blue-700 bg-blue-50 px-2 py-0.5 rounded">
            New
          </span>
        )}
      </div>
      <div className="text-xs text-gray-500 space-y-0.5">
        <div>IP: {cam.ip}</div>
        {cam.serial && <div>Serial: {cam.serial}</div>}
        {cam.firmware && <div>Firmware: {cam.firmware}</div>}
        {cam.mac && <div>MAC: {cam.mac}</div>}
      </div>
      {!cam.already_added && (
        <button
          onClick={() => onAdd(cam)}
          className="mt-2 w-full px-3 py-1 bg-blue-600 text-white rounded text-xs hover:bg-blue-700"
        >
          + Add to Cilex Vision
        </button>
      )}
    </div>
  );
}

function RescanPanel({
  onPrefill,
  onClose,
}: {
  onPrefill: (seed: { camera_id: string; name: string; rtsp_url: string }) => void;
  onClose: () => void;
}) {
  const [scanning, setScanning] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DiscoveryResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/streams/discover", {
          method: "POST",
          credentials: "include",
        });
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const data: DiscoveryResponse = await res.json();
        if (!cancelled) setResult(data);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Scan failed");
      } finally {
        if (!cancelled) setScanning(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="font-medium">Network scan</h2>
        <button
          onClick={onClose}
          className="text-xs text-gray-500 hover:text-gray-800"
        >
          Close
        </button>
      </div>
      {scanning && (
        <div className="flex items-center gap-2 text-sm text-gray-500">
          <div className="w-3 h-3 border-2 border-gray-300 border-t-blue-600 rounded-full animate-spin" />
          Scanning network via WS-Discovery (3 s)...
        </div>
      )}
      {error && (
        <div className="text-sm text-red-600">Scan failed: {error}</div>
      )}
      {result && !scanning && (
        <div className="space-y-2">
          <div className="text-xs text-gray-500">
            Found {result.cameras.length} camera(s) in {result.scan_time_ms} ms
          </div>
          {result.cameras.length === 0 && (
            <div className="text-sm text-gray-400">
              No cameras responded to the ONVIF probe. If the query-api container
              is on a Docker bridge network, UDP multicast won&apos;t reach the
              LAN — use the Add Camera form instead, or run query-api with host
              networking.
            </div>
          )}
          {result.cameras.length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
              {result.cameras.map((cam) => (
                <DiscoveredCard
                  key={cam.ip}
                  cam={cam}
                  onAdd={(c) =>
                    onPrefill({
                      camera_id: slugifyCameraId(c.model, c.ip),
                      name: c.device_name || c.model || c.ip,
                      rtsp_url: c.rtsp_url,
                    })
                  }
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function LivePage() {
  const [streams, setStreams] = useState<StreamInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [formSeed, setFormSeed] = useState<
    { camera_id: string; name: string; rtsp_url: string } | undefined
  >();
  const [showRescan, setShowRescan] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("name");
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [singleActiveId, setSingleActiveId] = useState<string | null>(null);
  const admin = isAdmin(getUserRole());

  useEffect(() => {
    const stored = typeof window !== "undefined" ? localStorage.getItem("live:viewMode") : null;
    if (stored === "grid" || stored === "single") setViewMode(stored);
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined") localStorage.setItem("live:viewMode", viewMode);
  }, [viewMode]);

  async function loadStreams() {
    try {
      const res = await fetch("/api/streams", { credentials: "include" });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      await loadStreams();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to remove camera");
    }
  }

  const filteredStreams = useMemo(() => {
    const q = query.trim().toLowerCase();
    let out = streams.filter((s) => {
      if (statusFilter === "online" && s.status !== "online") return false;
      if (statusFilter === "offline" && s.status === "online") return false;
      if (!q) return true;
      return (
        s.name.toLowerCase().includes(q) ||
        s.camera_id.toLowerCase().includes(q) ||
        s.status.toLowerCase().includes(q)
      );
    });
    out = [...out].sort((a, b) => {
      if (sortKey === "name") return (a.name || a.camera_id).localeCompare(b.name || b.camera_id);
      if (sortKey === "id") return a.camera_id.localeCompare(b.camera_id);
      return a.status.localeCompare(b.status);
    });
    return out;
  }, [streams, query, statusFilter, sortKey]);

  const expandedStream = expandedId
    ? streams.find((s) => s.camera_id === expandedId) ?? null
    : null;

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading cameras...</div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-xl font-semibold">Live Cameras</h1>
        <div className="flex items-center gap-2">
          <div className="flex border border-gray-300 rounded overflow-hidden text-sm">
            <button
              onClick={() => setViewMode("grid")}
              className={`px-2 py-1 ${viewMode === "grid" ? "bg-gray-800 text-white" : "bg-white hover:bg-gray-50"}`}
            >
              Grid
            </button>
            <button
              onClick={() => setViewMode("single")}
              className={`px-2 py-1 ${viewMode === "single" ? "bg-gray-800 text-white" : "bg-white hover:bg-gray-50"}`}
            >
              Single
            </button>
          </div>
          {admin && !showRescan && (
            <button
              onClick={() => setShowRescan(true)}
              className="px-3 py-1.5 border border-gray-300 rounded text-sm hover:bg-gray-50"
            >
              Rescan Network
            </button>
          )}
          {admin && !showForm && (
            <button
              onClick={() => {
                setFormSeed(undefined);
                setShowForm(true);
              }}
              className="px-3 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700"
            >
              + Add Camera
            </button>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search name, ID..."
          className="flex-1 min-w-[180px] border border-gray-300 rounded px-3 py-1.5"
        />
        <div className="flex border border-gray-300 rounded overflow-hidden">
          {(["all", "online", "offline"] as StatusFilter[]).map((f) => (
            <button
              key={f}
              onClick={() => setStatusFilter(f)}
              className={`px-3 py-1 capitalize ${
                statusFilter === f ? "bg-gray-800 text-white" : "bg-white hover:bg-gray-50"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <select
          value={sortKey}
          onChange={(e) => setSortKey(e.target.value as SortKey)}
          className="border border-gray-300 rounded px-2 py-1 bg-white"
        >
          <option value="name">Sort: Name</option>
          <option value="id">Sort: ID</option>
          <option value="status">Sort: Status</option>
        </select>
        <span className="text-xs text-gray-400">
          {filteredStreams.length} / {streams.length}
        </span>
      </div>

      {showRescan && (
        <RescanPanel
          onPrefill={(seed) => {
            setFormSeed(seed);
            setShowRescan(false);
            setShowForm(true);
          }}
          onClose={() => setShowRescan(false)}
        />
      )}

      {showForm && (
        <AddCameraForm
          initial={formSeed}
          onAdded={() => {
            setShowForm(false);
            setFormSeed(undefined);
            loadStreams();
          }}
          onCancel={() => {
            setShowForm(false);
            setFormSeed(undefined);
          }}
        />
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {filteredStreams.length === 0 && !error && (
        <div className="text-center py-12 text-gray-400">
          {streams.length === 0 ? "No cameras configured." : "No cameras match the filter."}
        </div>
      )}

      {filteredStreams.length > 0 && viewMode === "grid" && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {filteredStreams.map((s) => (
            <CameraCard
              key={s.camera_id}
              stream={s}
              admin={admin}
              onRemove={handleRemove}
              onExpand={setExpandedId}
            />
          ))}
        </div>
      )}

      {filteredStreams.length > 0 && viewMode === "single" && (
        <SingleView
          streams={filteredStreams}
          activeId={singleActiveId}
          onChange={setSingleActiveId}
        />
      )}

      {expandedStream && (
        <FullscreenView stream={expandedStream} onClose={() => setExpandedId(null)} />
      )}
    </div>
  );
}
