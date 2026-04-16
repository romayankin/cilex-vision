"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getStreamUrls } from "@/lib/stream-urls";

type Point = [number, number];

interface LoiteringZone {
  zone_id: string;
  polygon: Point[];
  duration_s: number;
}

interface ZoneConfig {
  camera_id: string;
  roi: Point[] | null;
  loitering_zones: LoiteringZone[];
}

interface StreamInfo {
  camera_id: string;
  name: string;
}

type DrawMode = "idle" | "drawing_roi" | "drawing_zone";

const ROI_COLOR = "#22c55e";
const ROI_FILL = "rgba(34, 197, 94, 0.15)";
const ZONE_COLOR = "#ef4444";
const ZONE_FILL = "rgba(239, 68, 68, 0.15)";
const DRAFT_COLOR = "#3b82f6";

const DEFAULT_ZONE_DURATION_S = 60;

function clamp01(v: number): number {
  return Math.max(0, Math.min(1, v));
}

function normalizedFromEvent(
  e: React.MouseEvent<SVGElement>,
  svg: SVGSVGElement | null,
): Point | null {
  const rect = svg?.getBoundingClientRect();
  if (!rect || rect.width === 0 || rect.height === 0) return null;
  return [
    clamp01((e.clientX - rect.left) / rect.width),
    clamp01((e.clientY - rect.top) / rect.height),
  ];
}

export default function ZonesPage() {
  const [streams, setStreams] = useState<StreamInfo[]>([]);
  const [selectedCamera, setSelectedCamera] = useState<string>("");
  const [roi, setRoi] = useState<Point[] | null>(null);
  const [zones, setZones] = useState<LoiteringZone[]>([]);
  const [draft, setDraft] = useState<Point[]>([]);
  const [drawMode, setDrawMode] = useState<DrawMode>("idle");
  const [dragTarget, setDragTarget] = useState<
    | { kind: "roi"; index: number }
    | { kind: "zone"; zoneIndex: number; index: number }
    | { kind: "draft"; index: number }
    | null
  >(null);
  const [saveStatus, setSaveStatus] = useState<
    { kind: "idle" } | { kind: "saving" } | { kind: "ok" } | { kind: "error"; msg: string }
  >({ kind: "idle" });
  const [snapshotKey, setSnapshotKey] = useState<number>(Date.now());
  const svgRef = useRef<SVGSVGElement | null>(null);

  // Load camera list
  useEffect(() => {
    let cancelled = false;
    fetch("/api/streams", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d) => {
        if (cancelled) return;
        const list: StreamInfo[] = (d.streams ?? []).map(
          (s: { camera_id: string; name: string }) => ({
            camera_id: s.camera_id,
            name: s.name || s.camera_id,
          }),
        );
        setStreams(list);
        if (list.length > 0) {
          setSelectedCamera((prev) => prev || list[0].camera_id);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Load zone config when camera changes
  useEffect(() => {
    if (!selectedCamera) return;
    let cancelled = false;
    fetch(`/api/cameras/${selectedCamera}/zones`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: ZoneConfig) => {
        if (cancelled) return;
        setRoi(d.roi ?? null);
        setZones(
          (d.loitering_zones ?? []).map((z) => ({
            zone_id: z.zone_id,
            polygon: z.polygon,
            duration_s: z.duration_s ?? DEFAULT_ZONE_DURATION_S,
          })),
        );
        setDraft([]);
        setDrawMode("idle");
        setSaveStatus({ kind: "idle" });
      })
      .catch(() => {
        if (!cancelled) {
          setRoi(null);
          setZones([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedCamera]);

  // Refresh snapshot every 10s
  useEffect(() => {
    if (!selectedCamera) return;
    const id = window.setInterval(() => setSnapshotKey(Date.now()), 10_000);
    return () => window.clearInterval(id);
  }, [selectedCamera]);

  const snapshotUrl = useMemo(() => {
    if (!selectedCamera) return null;
    const base = getStreamUrls(selectedCamera).snapshot_url;
    return `${base}&t=${snapshotKey}`;
  }, [selectedCamera, snapshotKey]);

  const handleCanvasClick = useCallback(
    (e: React.MouseEvent<SVGElement>) => {
      if (dragTarget !== null) return;
      if (drawMode === "idle") return;
      const pt = normalizedFromEvent(e, svgRef.current);
      if (!pt) return;
      setDraft((prev) => [...prev, pt]);
    },
    [drawMode, dragTarget],
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<SVGElement>) => {
      if (!dragTarget) return;
      const pt = normalizedFromEvent(e, svgRef.current);
      if (!pt) return;
      if (dragTarget.kind === "roi") {
        setRoi((prev) => {
          if (!prev) return prev;
          const copy = prev.slice();
          copy[dragTarget.index] = pt;
          return copy;
        });
      } else if (dragTarget.kind === "zone") {
        setZones((prev) => {
          const copy = prev.slice();
          const zone = copy[dragTarget.zoneIndex];
          if (!zone) return prev;
          const poly = zone.polygon.slice();
          poly[dragTarget.index] = pt;
          copy[dragTarget.zoneIndex] = { ...zone, polygon: poly };
          return copy;
        });
      } else if (dragTarget.kind === "draft") {
        setDraft((prev) => {
          const copy = prev.slice();
          copy[dragTarget.index] = pt;
          return copy;
        });
      }
    },
    [dragTarget],
  );

  const handleMouseUp = useCallback(() => setDragTarget(null), []);

  const startDrawRoi = () => {
    setDraft([]);
    setDrawMode("drawing_roi");
    setSaveStatus({ kind: "idle" });
  };

  const startDrawZone = () => {
    setDraft([]);
    setDrawMode("drawing_zone");
    setSaveStatus({ kind: "idle" });
  };

  const cancelDraft = () => {
    setDraft([]);
    setDrawMode("idle");
  };

  const finishDraft = () => {
    if (draft.length < 3) {
      setSaveStatus({
        kind: "error",
        msg: "Polygon needs at least 3 points",
      });
      return;
    }
    if (drawMode === "drawing_roi") {
      setRoi(draft);
    } else if (drawMode === "drawing_zone") {
      setZones((prev) => [
        ...prev,
        {
          zone_id: `zone-${Date.now()}`,
          polygon: draft,
          duration_s: DEFAULT_ZONE_DURATION_S,
        },
      ]);
    }
    setDraft([]);
    setDrawMode("idle");
  };

  const deleteRoi = () => setRoi(null);

  const deleteZone = (index: number) =>
    setZones((prev) => prev.filter((_, i) => i !== index));

  const updateZoneDuration = (index: number, duration: number) =>
    setZones((prev) => {
      const copy = prev.slice();
      if (copy[index]) {
        copy[index] = { ...copy[index], duration_s: Math.max(1, duration) };
      }
      return copy;
    });

  const updateZoneId = (index: number, zoneId: string) =>
    setZones((prev) => {
      const copy = prev.slice();
      if (copy[index]) {
        copy[index] = { ...copy[index], zone_id: zoneId };
      }
      return copy;
    });

  const removeVertex = (
    target:
      | { kind: "roi"; index: number }
      | { kind: "zone"; zoneIndex: number; index: number }
      | { kind: "draft"; index: number },
  ) => {
    if (target.kind === "roi") {
      setRoi((prev) => {
        if (!prev || prev.length <= 3) return prev;
        return prev.filter((_, i) => i !== target.index);
      });
    } else if (target.kind === "zone") {
      setZones((prev) => {
        const copy = prev.slice();
        const zone = copy[target.zoneIndex];
        if (!zone || zone.polygon.length <= 3) return prev;
        copy[target.zoneIndex] = {
          ...zone,
          polygon: zone.polygon.filter((_, i) => i !== target.index),
        };
        return copy;
      });
    } else {
      setDraft((prev) => prev.filter((_, i) => i !== target.index));
    }
  };

  const save = async () => {
    if (!selectedCamera) return;
    setSaveStatus({ kind: "saving" });
    try {
      const res = await fetch(`/api/cameras/${selectedCamera}/zones`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          roi: roi,
          loitering_zones: zones.map((z) => ({
            zone_id: z.zone_id,
            polygon: z.polygon,
            duration_s: z.duration_s,
          })),
        }),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(detail || `HTTP ${res.status}`);
      }
      setSaveStatus({ kind: "ok" });
    } catch (err) {
      setSaveStatus({
        kind: "error",
        msg: err instanceof Error ? err.message : String(err),
      });
    }
  };

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Zone Editor</h1>
        <p className="text-xs text-gray-500 mt-1">
          Draw ROI and loitering zones on camera snapshots. Coordinates are
          normalized (0-1) so zones work regardless of resolution.
        </p>
      </div>

      <div className="flex items-center gap-3">
        <label className="text-sm text-gray-600">Camera:</label>
        <select
          value={selectedCamera}
          onChange={(e) => setSelectedCamera(e.target.value)}
          className="text-sm border border-gray-300 rounded px-2 py-1 bg-white"
        >
          {streams.length === 0 && <option value="">No cameras</option>}
          {streams.map((s) => (
            <option key={s.camera_id} value={s.camera_id}>
              {s.name} ({s.camera_id})
            </option>
          ))}
        </select>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[2fr_1fr] gap-4">
        <div
          className="relative bg-gray-900 rounded-lg overflow-hidden select-none"
          style={{ aspectRatio: "16/9" }}
        >
          {snapshotUrl && (
            <img
              src={snapshotUrl}
              alt={`Snapshot of ${selectedCamera}`}
              className="absolute inset-0 w-full h-full object-cover"
              draggable={false}
            />
          )}
          <svg
            ref={svgRef}
            className={`absolute inset-0 w-full h-full ${
              drawMode !== "idle" ? "cursor-crosshair" : ""
            }`}
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            onClick={handleCanvasClick}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={handleMouseUp}
          >
            {roi && roi.length >= 3 && (
              <polygon
                points={roi.map(([x, y]) => `${x},${y}`).join(" ")}
                fill={ROI_FILL}
                stroke={ROI_COLOR}
                strokeWidth="0.003"
                vectorEffect="non-scaling-stroke"
              />
            )}

            {zones.map((zone, zi) =>
              zone.polygon.length >= 3 ? (
                <polygon
                  key={`zone-poly-${zi}`}
                  points={zone.polygon.map(([x, y]) => `${x},${y}`).join(" ")}
                  fill={ZONE_FILL}
                  stroke={ZONE_COLOR}
                  strokeWidth="0.003"
                  vectorEffect="non-scaling-stroke"
                />
              ) : null,
            )}

            {draft.length >= 2 && (
              <polyline
                points={draft.map(([x, y]) => `${x},${y}`).join(" ")}
                fill="none"
                stroke={DRAFT_COLOR}
                strokeWidth="0.003"
                strokeDasharray="0.01 0.005"
                vectorEffect="non-scaling-stroke"
              />
            )}

            {roi?.map(([x, y], i) => (
              <circle
                key={`roi-v-${i}`}
                cx={x}
                cy={y}
                r="0.01"
                fill="white"
                stroke={ROI_COLOR}
                strokeWidth="0.003"
                vectorEffect="non-scaling-stroke"
                style={{ cursor: "grab" }}
                onMouseDown={(e) => {
                  e.stopPropagation();
                  setDragTarget({ kind: "roi", index: i });
                }}
                onContextMenu={(e) => {
                  e.preventDefault();
                  removeVertex({ kind: "roi", index: i });
                }}
              />
            ))}

            {zones.map((zone, zi) =>
              zone.polygon.map(([x, y], i) => (
                <circle
                  key={`zone-${zi}-v-${i}`}
                  cx={x}
                  cy={y}
                  r="0.01"
                  fill="white"
                  stroke={ZONE_COLOR}
                  strokeWidth="0.003"
                  vectorEffect="non-scaling-stroke"
                  style={{ cursor: "grab" }}
                  onMouseDown={(e) => {
                    e.stopPropagation();
                    setDragTarget({ kind: "zone", zoneIndex: zi, index: i });
                  }}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    removeVertex({ kind: "zone", zoneIndex: zi, index: i });
                  }}
                />
              )),
            )}

            {draft.map(([x, y], i) => (
              <circle
                key={`draft-v-${i}`}
                cx={x}
                cy={y}
                r="0.01"
                fill={DRAFT_COLOR}
                stroke="white"
                strokeWidth="0.003"
                vectorEffect="non-scaling-stroke"
                style={{ cursor: "grab" }}
                onMouseDown={(e) => {
                  e.stopPropagation();
                  setDragTarget({ kind: "draft", index: i });
                }}
                onContextMenu={(e) => {
                  e.preventDefault();
                  removeVertex({ kind: "draft", index: i });
                }}
              />
            ))}
          </svg>

          {drawMode !== "idle" && (
            <div className="absolute top-2 left-2 bg-blue-600 text-white text-xs px-2 py-1 rounded shadow">
              {drawMode === "drawing_roi"
                ? "Drawing ROI — click to add points, Finish to save"
                : "Drawing loitering zone — click to add points, Finish to save"}
              <span className="ml-2 opacity-80">({draft.length} points)</span>
            </div>
          )}
        </div>

        <aside className="bg-white border border-gray-200 rounded-lg p-4 space-y-4">
          <div>
            <h2 className="font-medium text-sm">Zones</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Click on the image to add vertices. Drag to move them.
              Right-click a vertex to delete it.
            </p>
          </div>

          {drawMode !== "idle" ? (
            <div className="space-y-2 border border-blue-200 bg-blue-50 rounded p-3">
              <p className="text-xs text-blue-900">
                {draft.length < 3
                  ? `Click at least ${3 - draft.length} more point${
                      3 - draft.length === 1 ? "" : "s"
                    } on the snapshot.`
                  : `Polygon has ${draft.length} points — ready to finish.`}
              </p>
              <div className="flex gap-2">
                <button
                  onClick={finishDraft}
                  disabled={draft.length < 3}
                  className="flex-1 text-xs bg-blue-600 text-white px-3 py-1.5 rounded disabled:bg-gray-300 disabled:cursor-not-allowed"
                >
                  Finish
                </button>
                <button
                  onClick={cancelDraft}
                  className="flex-1 text-xs bg-white border border-gray-300 px-3 py-1.5 rounded hover:bg-gray-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between border border-gray-200 rounded p-2">
                <div className="flex items-center gap-2">
                  <span
                    className="inline-block w-3 h-3 rounded-sm"
                    style={{ background: ROI_COLOR }}
                  />
                  <span className="text-sm font-medium">ROI</span>
                  <span className="text-xs text-gray-500">
                    {roi ? `${roi.length} points` : "not set"}
                  </span>
                </div>
                {roi ? (
                  <button
                    onClick={deleteRoi}
                    className="text-xs text-red-600 hover:text-red-700"
                  >
                    Delete
                  </button>
                ) : (
                  <button
                    onClick={startDrawRoi}
                    className="text-xs text-blue-600 hover:text-blue-700"
                  >
                    Draw
                  </button>
                )}
              </div>

              {zones.map((zone, i) => (
                <div
                  key={`zone-row-${i}`}
                  className="border border-gray-200 rounded p-2 space-y-2"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span
                        className="inline-block w-3 h-3 rounded-sm"
                        style={{ background: ZONE_COLOR }}
                      />
                      <input
                        type="text"
                        value={zone.zone_id}
                        onChange={(e) => updateZoneId(i, e.target.value)}
                        className="text-sm font-medium border-b border-transparent hover:border-gray-300 focus:border-blue-500 focus:outline-none bg-transparent"
                      />
                    </div>
                    <button
                      onClick={() => deleteZone(i)}
                      className="text-xs text-red-600 hover:text-red-700"
                    >
                      Delete
                    </button>
                  </div>
                  <div className="flex items-center gap-2 text-xs text-gray-600">
                    <span>{zone.polygon.length} points</span>
                    <span>·</span>
                    <label className="flex items-center gap-1">
                      Duration:
                      <input
                        type="number"
                        min={1}
                        value={zone.duration_s}
                        onChange={(e) =>
                          updateZoneDuration(i, Number(e.target.value))
                        }
                        className="w-16 border border-gray-300 rounded px-1.5 py-0.5 text-xs"
                      />
                      s
                    </label>
                  </div>
                </div>
              ))}

              <button
                onClick={startDrawZone}
                className="w-full text-xs border border-dashed border-gray-300 rounded py-2 text-gray-600 hover:bg-gray-50"
              >
                + Add Loitering Zone
              </button>
            </div>
          )}

          <div className="border-t border-gray-200 pt-3 space-y-2">
            <button
              onClick={save}
              disabled={saveStatus.kind === "saving" || !selectedCamera}
              className="w-full text-sm bg-blue-600 text-white px-3 py-2 rounded disabled:bg-gray-300 disabled:cursor-not-allowed hover:bg-blue-700"
            >
              {saveStatus.kind === "saving" ? "Saving…" : "Save"}
            </button>
            {saveStatus.kind === "ok" && (
              <p className="text-xs text-green-700">Zones saved.</p>
            )}
            {saveStatus.kind === "error" && (
              <p className="text-xs text-red-700">
                Save failed: {saveStatus.msg}
              </p>
            )}
          </div>
        </aside>
      </div>

      <section className="bg-white border border-gray-200 rounded-lg p-4 text-sm text-gray-700 space-y-3">
        <h2 className="font-medium">How zones work</h2>
        <div>
          <p className="font-medium text-green-700">
            ROI (Region of Interest) — green polygon
          </p>
          <p className="text-xs text-gray-600 mt-1">
            Only events INSIDE this area are reported. Anything outside is
            ignored. If no ROI is drawn, the entire frame is the region of
            interest. Use this to exclude walls, sky, trees, or areas with
            constant motion.
          </p>
        </div>
        <div>
          <p className="font-medium text-red-700">Loitering Zone — red polygon(s)</p>
          <p className="text-xs text-gray-600 mt-1">
            If a person stays inside this zone longer than the specified
            duration, a <code className="bg-gray-100 px-1 rounded">loitering</code>{" "}
            event is triggered. Use for restricted areas, emergency exits, or
            anywhere people shouldn&apos;t linger.
          </p>
        </div>
        <p className="text-xs text-gray-500">
          Coordinates are normalized (0-1) relative to the frame. They work
          regardless of camera resolution or aspect ratio.
        </p>
      </section>
    </div>
  );
}
