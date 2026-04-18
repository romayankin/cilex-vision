"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ThermometerSun,
  Thermometer,
  ThermometerSnowflake,
  Save,
  ChevronDown,
  ChevronRight,
  HardDrive,
} from "lucide-react";
import {
  getStorageTierConfig,
  updateStorageTierConfig,
  getStorageTierUsage,
  type StorageTierConfig,
  type TierQuality,
  type TierUsageResponse,
} from "@/lib/api-client";
import { getUserRole, isAdmin } from "@/lib/auth";

const MIN_FRACTION = 0.05;
const MAX_FRACTION = 0.90;
const PILLAR_HEIGHT = 320;

type Tier = "hot" | "warm" | "cold";

const TIER_META: Record<
  Tier,
  { label: string; gradient: string; ring: string; chip: string; Icon: typeof ThermometerSun }
> = {
  hot: {
    label: "Hot",
    gradient: "from-red-500 to-orange-400",
    ring: "ring-red-300",
    chip: "bg-red-50 text-red-700 border-red-200",
    Icon: ThermometerSun,
  },
  warm: {
    label: "Warm",
    gradient: "from-amber-500 to-yellow-300",
    ring: "ring-amber-300",
    chip: "bg-amber-50 text-amber-700 border-amber-200",
    Icon: Thermometer,
  },
  cold: {
    label: "Cold",
    gradient: "from-cyan-600 to-blue-400",
    ring: "ring-cyan-300",
    chip: "bg-cyan-50 text-cyan-700 border-cyan-200",
    Icon: ThermometerSnowflake,
  },
};

const PRESETS: { key: string; label: string; desc: string; hot: number; warm: number; cold: number }[] = [
  { key: "balanced", label: "Balanced", desc: "20 / 30 / 50", hot: 0.20, warm: 0.30, cold: 0.50 },
  { key: "forensic", label: "Forensic", desc: "50 / 30 / 20", hot: 0.50, warm: 0.30, cold: 0.20 },
  { key: "archive",  label: "Archive",  desc: "10 / 20 / 70", hot: 0.10, warm: 0.20, cold: 0.70 },
];

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function redistribute(
  tier: Tier,
  newFrac: number,
  cur: Record<Tier, number>,
): Record<Tier, number> {
  newFrac = clamp(newFrac, MIN_FRACTION, MAX_FRACTION);
  const remaining = 1 - newFrac;
  const others: Tier[] = (["hot", "warm", "cold"] as Tier[]).filter((t) => t !== tier);
  const [a, b] = others;
  const sumOthers = cur[a] + cur[b];
  let aFrac = sumOthers > 0 ? remaining * (cur[a] / sumOthers) : remaining / 2;
  let bFrac = remaining - aFrac;

  if (aFrac < MIN_FRACTION) { aFrac = MIN_FRACTION; bFrac = remaining - MIN_FRACTION; }
  if (bFrac < MIN_FRACTION) { bFrac = MIN_FRACTION; aFrac = remaining - MIN_FRACTION; }

  // If after clamping the others can't fit, the dragged tier was too large.
  if (aFrac < MIN_FRACTION || bFrac < MIN_FRACTION) {
    const maxForTier = 1 - 2 * MIN_FRACTION;
    return redistribute(tier, maxForTier, cur);
  }

  const out = { ...cur };
  out[tier] = newFrac;
  out[a] = aFrac;
  out[b] = bFrac;
  return out;
}

function computeRetentionHours(gb: number, bitrateKbps: number, numCameras: number): number {
  const bytesPerSec = (bitrateKbps * 1000 / 8) * Math.max(numCameras, 1);
  if (bytesPerSec <= 0) return 0;
  const totalBytes = gb * 1024 ** 3;
  return totalBytes / bytesPerSec / 3600;
}

function prettyHours(hours: number): string {
  if (!isFinite(hours) || hours <= 0) return "0m";
  if (hours < 1) return `${Math.floor(hours * 60)}m`;
  const days = Math.floor(hours / 24);
  const remHours = Math.floor(hours % 24);
  const mins = Math.floor((hours - Math.floor(hours)) * 60);
  if (days > 0) return `${days}d ${remHours}h`;
  if (remHours > 0) return `${remHours}h ${mins}m`;
  return `${mins}m`;
}

function bytesToGb(b: number): number {
  return b / 1024 ** 3;
}

interface PillarProps {
  tier: Tier;
  fraction: number;
  gb: number;
  retention: string;
  onDrag: (tier: Tier, newFrac: number) => void;
}

function Pillar({ tier, fraction, gb, retention, onDrag }: PillarProps) {
  const meta = TIER_META[tier];
  const fillHeight = PILLAR_HEIGHT * fraction;
  const dragRef = useRef<{ startY: number; startFrac: number } | null>(null);

  const handleStart = (clientY: number) => {
    dragRef.current = { startY: clientY, startFrac: fraction };
  };

  const handleMove = useCallback(
    (clientY: number) => {
      if (!dragRef.current) return;
      const delta = dragRef.current.startY - clientY; // up = positive
      const fracDelta = delta / PILLAR_HEIGHT;
      const newFrac = dragRef.current.startFrac + fracDelta;
      onDrag(tier, newFrac);
    },
    [tier, onDrag],
  );

  const handleEnd = () => {
    dragRef.current = null;
  };

  useEffect(() => {
    const onMove = (e: MouseEvent) => handleMove(e.clientY);
    const onUp = () => handleEnd();
    const onTouchMove = (e: TouchEvent) => {
      if (e.touches[0]) handleMove(e.touches[0].clientY);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    document.addEventListener("touchmove", onTouchMove, { passive: true });
    document.addEventListener("touchend", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.removeEventListener("touchmove", onTouchMove);
      document.removeEventListener("touchend", onUp);
    };
  }, [handleMove]);

  return (
    <div className="flex flex-col items-center gap-2 select-none">
      <div className="flex items-center gap-1.5 text-sm font-medium text-gray-700">
        <meta.Icon className="w-4 h-4" />
        <span>{meta.label}</span>
      </div>

      <div
        className="relative bg-gray-100 rounded-lg overflow-hidden border border-gray-200 w-24"
        style={{ height: PILLAR_HEIGHT }}
      >
        <div
          className={`absolute bottom-0 left-0 right-0 bg-gradient-to-t ${meta.gradient} transition-[height] ease-out`}
          style={{ height: fillHeight, transitionDuration: dragRef.current ? "0ms" : "120ms" }}
        >
          <div
            className="absolute -top-1.5 left-0 right-0 h-3 cursor-ns-resize flex items-center justify-center"
            onMouseDown={(e) => { e.preventDefault(); handleStart(e.clientY); }}
            onTouchStart={(e) => { if (e.touches[0]) handleStart(e.touches[0].clientY); }}
            title="Drag to resize"
          >
            <div className="w-10 h-1 rounded-full bg-white/80 shadow-sm" />
          </div>

          <div className="absolute inset-x-0 bottom-2 text-center text-white font-semibold text-lg drop-shadow">
            {Math.round(fraction * 100)}%
          </div>
        </div>
      </div>

      <div className="text-center">
        <div className="text-sm font-medium text-gray-900">{gb.toFixed(1)} GB</div>
        <div className="text-xs text-gray-500">{retention}</div>
      </div>
    </div>
  );
}

interface QualityEditorProps {
  tier: Tier;
  value: TierQuality;
  onChange: (next: TierQuality) => void;
}

function QualityEditor({ tier, value, onChange }: QualityEditorProps) {
  const meta = TIER_META[tier];
  const update = <K extends keyof TierQuality>(k: K, v: number) =>
    onChange({ ...value, [k]: v });

  return (
    <div className={`border rounded-lg p-3 ${meta.chip}`}>
      <div className="flex items-center gap-1.5 text-sm font-medium mb-2">
        <meta.Icon className="w-4 h-4" />
        <span>{meta.label}</span>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <label className="flex flex-col gap-0.5">
          <span className="text-gray-600">Width</span>
          <input
            type="number"
            value={value.width}
            onChange={(e) => update("width", parseInt(e.target.value || "0", 10))}
            className="border border-gray-300 rounded px-2 py-1 bg-white"
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-gray-600">Height</span>
          <input
            type="number"
            value={value.height}
            onChange={(e) => update("height", parseInt(e.target.value || "0", 10))}
            className="border border-gray-300 rounded px-2 py-1 bg-white"
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-gray-600">FPS</span>
          <input
            type="number"
            value={value.fps}
            onChange={(e) => update("fps", parseInt(e.target.value || "0", 10))}
            className="border border-gray-300 rounded px-2 py-1 bg-white"
          />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-gray-600">Bitrate (kbps)</span>
          <input
            type="number"
            value={value.bitrate_kbps}
            onChange={(e) => update("bitrate_kbps", parseInt(e.target.value || "0", 10))}
            className="border border-gray-300 rounded px-2 py-1 bg-white"
          />
        </label>
      </div>
    </div>
  );
}

export default function StorageTiersPage() {
  const role = getUserRole();
  const [config, setConfig] = useState<StorageTierConfig | null>(null);
  const [original, setOriginal] = useState<StorageTierConfig | null>(null);
  const [numCameras, setNumCameras] = useState<number>(2);
  const [diskTotal, setDiskTotal] = useState<number>(0);
  const [diskAvail, setDiskAvail] = useState<number>(0);
  const [usage, setUsage] = useState<TierUsageResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState<boolean>(false);

  useEffect(() => {
    if (!isAdmin(role)) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await getStorageTierConfig();
        if (cancelled) return;
        setConfig(data.config);
        setOriginal(data.config);
        setNumCameras(data.num_cameras);
        setDiskTotal(data.disk_total_gb);
        setDiskAvail(data.disk_available_gb);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load config");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [role]);

  useEffect(() => {
    if (!isAdmin(role)) return;
    let cancelled = false;
    const load = async () => {
      try {
        const u = await getStorageTierUsage();
        if (!cancelled) setUsage(u);
      } catch {
        // non-fatal
      }
    };
    load();
    const id = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [role]);

  const dirty = useMemo(() => {
    if (!config || !original) return false;
    return JSON.stringify(config) !== JSON.stringify(original);
  }, [config, original]);

  const handlePillarDrag = useCallback((tier: Tier, newFrac: number) => {
    setConfig((c) => {
      if (!c) return c;
      const next = redistribute(
        tier, newFrac,
        { hot: c.hot_fraction, warm: c.warm_fraction, cold: c.cold_fraction },
      );
      return { ...c, hot_fraction: next.hot, warm_fraction: next.warm, cold_fraction: next.cold };
    });
  }, []);

  const applyPreset = (p: typeof PRESETS[number]) => {
    setConfig((c) => c ? { ...c, hot_fraction: p.hot, warm_fraction: p.warm, cold_fraction: p.cold } : c);
  };

  const handleSave = async () => {
    if (!config) return;
    setSaving(true);
    setError(null);
    setSavedNote(null);
    try {
      await updateStorageTierConfig(config);
      setOriginal(config);
      setSavedNote("Saved. Rebalance will run during next idle period.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }
  if (loading) return <p className="text-sm text-gray-500">Loading storage configuration…</p>;
  if (!config) return <p className="text-sm text-red-600">{error ?? "Failed to load configuration."}</p>;

  const hotGb = config.total_budget_gb * config.hot_fraction;
  const warmGb = config.total_budget_gb * config.warm_fraction;
  const coldGb = config.total_budget_gb * config.cold_fraction;
  const hotHours = computeRetentionHours(hotGb, config.hot.bitrate_kbps, numCameras);
  const warmHours = computeRetentionHours(warmGb, config.warm.bitrate_kbps, numCameras);
  const coldHours = computeRetentionHours(coldGb, config.cold.bitrate_kbps, numCameras);

  const overBudget = config.total_budget_gb > diskAvail + (usage ? bytesToGb(
    (usage.hot.bytes + usage.warm.bytes + usage.cold.bytes),
  ) : 0);

  return (
    <div className="space-y-6 pb-12">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold text-gray-900">Storage Tiers</h1>
        <span className="text-xs text-gray-500">
          Hot/warm/cold video retention · {numCameras} camera{numCameras === 1 ? "" : "s"}
        </span>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">{error}</div>
      )}
      {savedNote && (
        <div className="bg-green-50 border border-green-200 text-green-700 text-sm rounded p-3">{savedNote}</div>
      )}

      {/* Top: budget + presets + backend */}
      <div className="bg-white border border-gray-200 rounded-lg p-4 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs text-gray-600 mb-1">Total video storage budget</label>
            <div className="flex items-center">
              <input
                type="number"
                min={1}
                value={config.total_budget_gb}
                onChange={(e) =>
                  setConfig({ ...config, total_budget_gb: parseFloat(e.target.value || "0") })
                }
                className="border border-gray-300 rounded-l px-3 py-1.5 text-sm w-full"
              />
              <span className="px-3 py-1.5 bg-gray-100 border border-l-0 border-gray-300 rounded-r text-sm text-gray-600">
                GB
              </span>
            </div>
            <div className="text-xs text-gray-500 mt-1 flex items-center gap-1">
              <HardDrive className="w-3 h-3" />
              Disk: {diskAvail.toFixed(1)} / {diskTotal.toFixed(1)} GB available
              {overBudget && <span className="text-red-600 font-medium ml-1">(budget exceeds free space)</span>}
            </div>
          </div>

          <div className="md:col-span-2">
            <label className="block text-xs text-gray-600 mb-1">Quick presets</label>
            <div className="flex gap-2 flex-wrap">
              {PRESETS.map((p) => (
                <button
                  key={p.key}
                  onClick={() => applyPreset(p)}
                  className="px-3 py-1.5 border border-gray-300 rounded text-xs hover:bg-gray-50 hover:border-gray-400"
                >
                  <div className="font-medium text-gray-900">{p.label}</div>
                  <div className="text-gray-500">{p.desc}</div>
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="border-t border-gray-100 pt-4">
          <div className="text-xs text-gray-600 mb-2">Storage backend</div>
          <div className="flex gap-4 text-sm">
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="backend"
                value="volume"
                checked={config.storage_backend === "volume"}
                onChange={() => setConfig({ ...config, storage_backend: "volume" })}
              />
              Docker volume (default)
            </label>
            <label className="flex items-center gap-2">
              <input
                type="radio"
                name="backend"
                value="bind"
                checked={config.storage_backend === "bind"}
                onChange={() => setConfig({ ...config, storage_backend: "bind" })}
              />
              Bind mount path
            </label>
            {config.storage_backend === "bind" && (
              <input
                type="text"
                placeholder="/mnt/video-storage"
                value={config.bind_mount_path ?? ""}
                onChange={(e) => setConfig({ ...config, bind_mount_path: e.target.value || null })}
                className="border border-gray-300 rounded px-2 py-1 text-sm flex-1 max-w-md font-mono"
              />
            )}
          </div>
          {config.storage_backend === "bind" && !config.bind_mount_path && (
            <div className="text-xs text-amber-600 mt-1">Path required when using bind mount.</div>
          )}
        </div>
      </div>

      {/* Pillars */}
      <div className="bg-white border border-gray-200 rounded-lg p-6">
        <div className="text-xs text-gray-500 mb-4">
          Drag the top of each pillar to resize. Minimum {MIN_FRACTION * 100}% per tier.
        </div>
        <div className="flex gap-12 justify-center">
          <Pillar tier="hot"  fraction={config.hot_fraction}  gb={hotGb}  retention={prettyHours(hotHours)}  onDrag={handlePillarDrag} />
          <Pillar tier="warm" fraction={config.warm_fraction} gb={warmGb} retention={prettyHours(warmHours)} onDrag={handlePillarDrag} />
          <Pillar tier="cold" fraction={config.cold_fraction} gb={coldGb} retention={prettyHours(coldHours)} onDrag={handlePillarDrag} />
        </div>
      </div>

      {/* Advanced quality */}
      <div className="bg-white border border-gray-200 rounded-lg">
        <button
          onClick={() => setAdvancedOpen((v) => !v)}
          className="w-full flex items-center gap-2 px-4 py-3 text-sm font-medium text-gray-700 hover:bg-gray-50"
        >
          {advancedOpen ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          Advanced — per-tier quality (resolution, fps, bitrate)
        </button>
        {advancedOpen && (
          <div className="px-4 pb-4 grid grid-cols-1 md:grid-cols-3 gap-3">
            <QualityEditor tier="hot"  value={config.hot}  onChange={(q) => setConfig({ ...config, hot: q })} />
            <QualityEditor tier="warm" value={config.warm} onChange={(q) => setConfig({ ...config, warm: q })} />
            <QualityEditor tier="cold" value={config.cold} onChange={(q) => setConfig({ ...config, cold: q })} />
          </div>
        )}
      </div>

      {/* Apply */}
      <div className="flex items-center justify-end gap-3">
        {dirty && <span className="text-xs text-amber-600">Unsaved changes</span>}
        <button
          onClick={handleSave}
          disabled={!dirty || saving}
          className="inline-flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
        >
          <Save className="w-4 h-4" />
          {saving ? "Saving…" : "Apply"}
        </button>
      </div>

      {/* Current usage */}
      <div className="bg-white border border-gray-200 rounded-lg p-4">
        <h2 className="text-sm font-semibold text-gray-900 mb-3">Current usage</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {(["hot", "warm", "cold"] as Tier[]).map((tier) => {
            const meta = TIER_META[tier];
            const usedGb = usage ? bytesToGb(usage[tier].bytes) : 0;
            const budgetGb = config.total_budget_gb * (
              tier === "hot" ? config.hot_fraction
              : tier === "warm" ? config.warm_fraction
              : config.cold_fraction
            );
            const pct = budgetGb > 0 ? Math.min(100, (usedGb / budgetGb) * 100) : 0;
            const segCount = usage?.[tier].segments ?? 0;
            const oldest = usage?.[tier].oldest;
            return (
              <div key={tier} className={`border rounded-lg p-3 ${meta.chip}`}>
                <div className="flex items-center gap-1.5 text-sm font-medium mb-2">
                  <meta.Icon className="w-4 h-4" />
                  <span>{meta.label}</span>
                </div>
                <div className="text-xs text-gray-700 mb-1">
                  {usedGb.toFixed(2)} / {budgetGb.toFixed(1)} GB
                </div>
                <div className="h-2 bg-white/70 rounded overflow-hidden mb-2">
                  <div
                    className={`h-full bg-gradient-to-r ${meta.gradient}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="text-xs text-gray-600">
                  {segCount.toLocaleString()} segment{segCount === 1 ? "" : "s"}
                </div>
                {oldest && (
                  <div className="text-xs text-gray-500 mt-0.5">
                    Oldest: {new Date(oldest).toLocaleString()}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
