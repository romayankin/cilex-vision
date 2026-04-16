"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import ClockPicker from "./ClockPicker";
import {
  OBJECT_CLASS_ICONS,
  EVENT_TYPE_ICONS,
  TRACK_STATE_ICONS,
  CameraGroupIcon,
  ObjectClassGroupIcon,
  ColorsGroupIcon,
  EventGroupIcon,
  TrackStateGroupIcon,
  TimeGroupIcon,
  ThumbnailIcon,
} from "./Pictograms";

const OBJECT_CLASSES = [
  "person",
  "car",
  "truck",
  "bus",
  "bicycle",
  "motorcycle",
  "animal",
];
const COLORS = [
  "red",
  "blue",
  "white",
  "black",
  "silver",
  "green",
  "yellow",
  "brown",
  "orange",
  "unknown",
];
const EVENT_TYPES = ["entered_scene", "exited_scene", "stopped", "loitering"];
const TRACK_STATES = ["new", "active", "lost", "terminated"];

const COLOR_MAP: Record<string, string> = {
  red: "#EF4444",
  blue: "#3B82F6",
  white: "#FFFFFF",
  black: "#111827",
  silver: "#9CA3AF",
  green: "#22C55E",
  yellow: "#EAB308",
  brown: "#92400E",
  orange: "#F97316",
  unknown: "#D1D5DB",
};

const LABELS: Record<string, string> = {
  entered_scene: "enter",
  exited_scene: "exit",
  stopped: "stop",
  loitering: "loiter",
  new: "new",
  active: "active",
  lost: "lost",
  terminated: "term.",
  person: "person",
  car: "car",
  truck: "truck",
  bus: "bus",
  bicycle: "bike",
  motorcycle: "moto",
  animal: "animal",
};

export interface FilterState {
  camera_id: string;
  start: string;
  end: string;
  object_class: string;
  color: string;
  event_type: string;
  state: string;
}

type GroupId = "camera" | "class" | "color" | "event" | "state" | "time";

interface StreamInfo {
  camera_id: string;
  name: string;
}

interface FilterSidebarProps {
  filters: FilterState;
  onChange: (filters: FilterState) => void;
  thumbOnly: boolean;
  onThumbOnlyChange: (v: boolean) => void;
}

function toSet(csv: string): Set<string> {
  return new Set(
    csv
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

function fromSet(set: Set<string>): string {
  return Array.from(set).join(",");
}

function splitDt(dt: string): { date: string; time: string } {
  if (!dt) return { date: "", time: "" };
  const [date, time] = dt.split("T");
  return { date: date ?? "", time: (time ?? "").slice(0, 5) };
}

function joinDt(date: string, time: string): string {
  if (!date && !time) return "";
  if (!date) return "";
  return `${date}T${time || "00:00"}`;
}

const GROUPS: { id: GroupId; title: string; Icon: (p: { className?: string }) => JSX.Element }[] = [
  { id: "camera", title: "Camera", Icon: CameraGroupIcon },
  { id: "class", title: "Object class", Icon: ObjectClassGroupIcon },
  { id: "color", title: "Colors", Icon: ColorsGroupIcon },
  { id: "event", title: "Event type", Icon: EventGroupIcon },
  { id: "state", title: "Track state", Icon: TrackStateGroupIcon },
  { id: "time", title: "Time range", Icon: TimeGroupIcon },
];

export default function FilterSidebar({
  filters,
  onChange,
  thumbOnly,
  onThumbOnlyChange,
}: FilterSidebarProps) {
  const [open, setOpen] = useState<GroupId | null>(null);
  const [streams, setStreams] = useState<StreamInfo[]>([]);
  const [streamsFailed, setStreamsFailed] = useState(false);
  const [classCounts, setClassCounts] = useState<Record<string, number>>({});

  useEffect(() => {
    let cancelled = false;
    fetch("/api/streams", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d) => {
        if (cancelled) return;
        const list: StreamInfo[] = (d.streams ?? []).map(
          (s: { camera_id: string; name: string }) => ({
            camera_id: s.camera_id,
            name: s.name,
          }),
        );
        setStreams(list);
      })
      .catch(() => {
        if (!cancelled) setStreamsFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const url = filters.camera_id
      ? `/api/detections/counts?camera_id=${encodeURIComponent(filters.camera_id)}`
      : "/api/detections/counts";
    fetch(url, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : {}))
      .then((d) => {
        if (!cancelled) setClassCounts(d ?? {});
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [filters.camera_id]);

  const set = useCallback(
    (key: keyof FilterState, value: string) => {
      onChange({ ...filters, [key]: value });
    },
    [filters, onChange],
  );

  const toggle = useCallback(
    (key: keyof FilterState, value: string) => {
      const s = toSet(filters[key]);
      if (s.has(value)) s.delete(value);
      else s.add(value);
      set(key, fromSet(s));
    },
    [filters, set],
  );

  const selectedCameras = useMemo(() => toSet(filters.camera_id), [filters.camera_id]);
  const selectedClasses = useMemo(() => toSet(filters.object_class), [filters.object_class]);
  const selectedColors = useMemo(() => toSet(filters.color), [filters.color]);
  const selectedEvents = useMemo(() => toSet(filters.event_type), [filters.event_type]);
  const selectedStates = useMemo(() => toSet(filters.state), [filters.state]);

  const groupActive: Record<GroupId, boolean> = {
    camera: selectedCameras.size > 0,
    class: selectedClasses.size > 0,
    color: selectedColors.size > 0,
    event: selectedEvents.size > 0,
    state: selectedStates.size > 0,
    time: Boolean(filters.start || filters.end),
  };

  const toggleGroup = (id: GroupId) => setOpen((prev) => (prev === id ? null : id));

  const startParts = splitDt(filters.start);
  const endParts = splitDt(filters.end);

  return (
    <div className="relative flex items-start">
      {/* Flyout panel (to the left of the icon bar) */}
      {open && (
        <div className="w-64 bg-white border border-gray-200 rounded-l-lg shadow-sm p-3 mr-[-1px] max-h-[calc(100vh-10rem)] overflow-y-auto">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-semibold text-gray-600 uppercase tracking-wider">
              {GROUPS.find((g) => g.id === open)?.title}
            </h3>
            <button
              type="button"
              onClick={() => setOpen(null)}
              className="text-gray-400 hover:text-gray-700 text-xs"
              aria-label="Close panel"
            >
              ✕
            </button>
          </div>

          {open === "camera" && (
            <div className="space-y-2">
              {streamsFailed ? (
                <input
                  type="text"
                  value={filters.camera_id}
                  onChange={(e) => set("camera_id", e.target.value)}
                  placeholder="e.g. cam-1 (comma-separated)"
                  className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              ) : (
                <>
                  <button
                    type="button"
                    onClick={() => set("camera_id", "")}
                    className={`w-full text-left px-2 py-1.5 text-xs rounded border transition ${
                      selectedCameras.size === 0
                        ? "bg-gray-900 text-white border-gray-900"
                        : "bg-white text-gray-600 border-gray-300 hover:border-gray-500"
                    }`}
                  >
                    All cameras
                  </button>
                  {streams.map((s) => {
                    const active = selectedCameras.has(s.camera_id);
                    return (
                      <button
                        key={s.camera_id}
                        type="button"
                        onClick={() => toggle("camera_id", s.camera_id)}
                        className={`w-full text-left px-2 py-1.5 text-xs rounded border transition font-mono flex items-center justify-between ${
                          active
                            ? "bg-blue-600 text-white border-blue-600"
                            : "bg-white text-gray-700 border-gray-300 hover:border-blue-500"
                        }`}
                      >
                        <span>{s.camera_id}</span>
                        <span
                          className={`font-sans text-[10px] truncate ml-2 ${
                            active ? "text-blue-100" : "text-gray-400"
                          }`}
                        >
                          {s.name}
                        </span>
                      </button>
                    );
                  })}
                  {streams.length === 0 && (
                    <span className="text-xs text-gray-400 italic">
                      No cameras configured
                    </span>
                  )}
                </>
              )}
            </div>
          )}

          {open === "class" && (
            <div className="grid grid-cols-2 gap-2">
              {OBJECT_CLASSES.map((v) => {
                const Icon = OBJECT_CLASS_ICONS[v];
                const active = selectedClasses.has(v);
                return (
                  <button
                    key={v}
                    type="button"
                    onClick={() => toggle("object_class", v)}
                    aria-pressed={active}
                    className={`group flex flex-col items-center justify-center gap-1 p-2 rounded border-2 transition ${
                      active
                        ? "border-blue-600 bg-blue-50 text-blue-900"
                        : "border-gray-200 bg-gray-50 text-gray-500 hover:border-gray-400 hover:bg-white"
                    }`}
                  >
                    {Icon && (
                      <Icon
                        className={`w-7 h-7 ${
                          active ? "text-blue-700" : "text-gray-500 group-hover:text-gray-800"
                        }`}
                      />
                    )}
                    <span
                      className={`text-[10px] leading-tight font-medium ${
                        active ? "text-blue-900" : "text-gray-600"
                      }`}
                    >
                      {LABELS[v] ?? v}
                      {classCounts[v] != null && (
                        <span className="ml-1 text-gray-400 font-normal">
                          ({classCounts[v].toLocaleString()})
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          {open === "color" && (
            <div className="grid grid-cols-5 gap-2">
              {COLORS.map((c) => {
                const active = selectedColors.has(c);
                const bg = COLOR_MAP[c];
                const isLight =
                  c === "white" || c === "silver" || c === "yellow" || c === "unknown";
                return (
                  <button
                    key={c}
                    type="button"
                    onClick={() => toggle("color", c)}
                    title={c}
                    aria-pressed={active}
                    className="flex flex-col items-center gap-1"
                  >
                    <span
                      className={`relative inline-flex items-center justify-center rounded-full transition-all ${
                        active ? "ring-2 ring-blue-600 ring-offset-1 scale-110" : ""
                      }`}
                      style={{
                        width: 28,
                        height: 28,
                        background: bg,
                        border: c === "white" ? "2px solid #D1D5DB" : "2px solid #FFFFFF",
                        boxShadow: "0 0 0 1px rgba(0,0,0,0.08)",
                      }}
                    >
                      {active && (
                        <svg
                          viewBox="0 0 20 20"
                          width="14"
                          height="14"
                          fill="none"
                          stroke={isLight ? "#111827" : "#FFFFFF"}
                          strokeWidth="3"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <polyline points="4,10 8,14 16,6" />
                        </svg>
                      )}
                    </span>
                    <span className="text-[9px] text-gray-600 capitalize">{c}</span>
                  </button>
                );
              })}
            </div>
          )}

          {open === "event" && (
            <div className="grid grid-cols-2 gap-2">
              {EVENT_TYPES.map((v) => {
                const Icon = EVENT_TYPE_ICONS[v];
                const active = selectedEvents.has(v);
                return (
                  <button
                    key={v}
                    type="button"
                    onClick={() => toggle("event_type", v)}
                    aria-pressed={active}
                    className={`group flex flex-col items-center justify-center gap-1 p-2 rounded border-2 transition ${
                      active
                        ? "border-blue-600 bg-blue-50 text-blue-900"
                        : "border-gray-200 bg-gray-50 text-gray-500 hover:border-gray-400 hover:bg-white"
                    }`}
                  >
                    {Icon && (
                      <Icon
                        className={`w-7 h-7 ${
                          active ? "text-blue-700" : "text-gray-500 group-hover:text-gray-800"
                        }`}
                      />
                    )}
                    <span
                      className={`text-[10px] leading-tight font-medium ${
                        active ? "text-blue-900" : "text-gray-600"
                      }`}
                    >
                      {LABELS[v] ?? v}
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          {open === "state" && (
            <div className="grid grid-cols-2 gap-2">
              {TRACK_STATES.map((v) => {
                const Icon = TRACK_STATE_ICONS[v];
                const active = selectedStates.has(v);
                return (
                  <button
                    key={v}
                    type="button"
                    onClick={() => toggle("state", v)}
                    aria-pressed={active}
                    className={`group flex flex-col items-center justify-center gap-1 p-2 rounded border-2 transition ${
                      active
                        ? "border-blue-600 bg-blue-50 text-blue-900"
                        : "border-gray-200 bg-gray-50 text-gray-500 hover:border-gray-400 hover:bg-white"
                    }`}
                  >
                    {Icon && (
                      <Icon
                        className={`w-7 h-7 ${
                          active ? "text-blue-700" : "text-gray-500 group-hover:text-gray-800"
                        }`}
                      />
                    )}
                    <span
                      className={`text-[10px] leading-tight font-medium ${
                        active ? "text-blue-900" : "text-gray-600"
                      }`}
                    >
                      {LABELS[v] ?? v}
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          {open === "time" && (
            <div className="space-y-3">
              <div className="space-y-1">
                <span className="text-[10px] text-gray-500 uppercase tracking-wide">
                  Start
                </span>
                <input
                  type="date"
                  value={startParts.date}
                  onChange={(e) => set("start", joinDt(e.target.value, startParts.time))}
                  className="w-full border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <ClockPicker
                  value={startParts.time}
                  onChange={(t) => set("start", joinDt(startParts.date, t))}
                />
              </div>
              <div className="space-y-1">
                <span className="text-[10px] text-gray-500 uppercase tracking-wide">
                  End
                </span>
                <input
                  type="date"
                  value={endParts.date}
                  onChange={(e) => set("end", joinDt(e.target.value, endParts.time))}
                  className="w-full border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <ClockPicker
                  value={endParts.time}
                  onChange={(t) => set("end", joinDt(endParts.date, t))}
                />
              </div>
              {(filters.start || filters.end) && (
                <button
                  type="button"
                  onClick={() => onChange({ ...filters, start: "", end: "" })}
                  className="w-full text-xs text-gray-500 hover:text-gray-900 border border-gray-200 rounded py-1"
                >
                  Clear time range
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* Icon bar */}
      <div className="w-11 bg-white border border-gray-200 rounded-lg flex flex-col items-center py-2 gap-1 self-start">
        {GROUPS.map(({ id, title, Icon }) => {
          const isOpen = open === id;
          const hasActive = groupActive[id];
          return (
            <button
              key={id}
              type="button"
              onClick={() => toggleGroup(id)}
              title={title}
              aria-label={title}
              aria-pressed={isOpen}
              className={`relative w-9 h-9 rounded flex items-center justify-center transition ${
                isOpen
                  ? "bg-blue-600 text-white"
                  : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
              }`}
            >
              <Icon className="w-6 h-6" />
              {hasActive && !isOpen && (
                <span className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full bg-blue-500" />
              )}
            </button>
          );
        })}

        <div className="w-7 h-px bg-gray-200 my-1" />

        <button
          type="button"
          onClick={() => onThumbOnlyChange(!thumbOnly)}
          title={thumbOnly ? "Thumbnails only — ON" : "Thumbnails only — OFF"}
          aria-label="Toggle thumbnails only"
          aria-pressed={thumbOnly}
          className={`relative w-9 h-9 rounded flex items-center justify-center transition ${
            thumbOnly
              ? "bg-green-600 text-white"
              : "text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          }`}
        >
          <ThumbnailIcon className="w-6 h-6" />
        </button>
      </div>
    </div>
  );
}
