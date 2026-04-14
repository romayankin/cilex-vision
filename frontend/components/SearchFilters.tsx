"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import ClockPicker from "./ClockPicker";
import {
  OBJECT_CLASS_ICONS,
  EVENT_TYPE_ICONS,
  TRACK_STATE_ICONS,
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
const EVENT_TYPES = [
  "entered_scene",
  "exited_scene",
  "stopped",
  "loitering",
  "motion_started",
  "motion_ended",
];
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
  motion_started: "start",
  motion_ended: "ended",
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
  start: string; // "YYYY-MM-DDTHH:MM"
  end: string;
  object_class: string;
  color: string;
  event_type: string;
  state: string;
}

interface StreamInfo {
  camera_id: string;
  name: string;
}

interface SearchFiltersProps {
  filters: FilterState;
  onChange: (filters: FilterState) => void;
  onSearch: () => void;
  thumbOnly?: boolean;
  onThumbOnlyChange?: (v: boolean) => void;
}

// Helpers for comma-separated multi-select
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

export default function SearchFilters({
  filters,
  onChange,
  onSearch,
  thumbOnly,
  onThumbOnlyChange,
}: SearchFiltersProps) {
  const [streams, setStreams] = useState<StreamInfo[]>([]);
  const [streamsFailed, setStreamsFailed] = useState(false);
  const [classCounts, setClassCounts] = useState<Record<string, number>>({});

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

  const startParts = splitDt(filters.start);
  const endParts = splitDt(filters.end);

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4 space-y-5">
      <h2 className="font-semibold text-sm text-gray-700 uppercase tracking-wide">
        Filters
      </h2>

      {/* Camera + time window */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Camera chips */}
        <div>
          <label className="block text-xs text-gray-500 mb-1.5 uppercase tracking-wide">
            Camera
          </label>
          {streamsFailed ? (
            <input
              type="text"
              value={filters.camera_id}
              onChange={(e) => set("camera_id", e.target.value)}
              placeholder="e.g. cam-1 (comma-separated for multiple)"
              className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          ) : (
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => set("camera_id", "")}
                className={`px-3 py-1 text-xs rounded-full border transition ${
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
                    className={`px-3 py-1 text-xs rounded-full border transition font-mono ${
                      active
                        ? "bg-blue-600 text-white border-blue-600"
                        : "bg-white text-gray-700 border-gray-300 hover:border-blue-500"
                    }`}
                  >
                    {s.camera_id}
                    <span
                      className={`ml-1 font-sans ${
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
            </div>
          )}
        </div>

        {/* Time window */}
        <div>
          <label className="block text-xs text-gray-500 mb-1.5 uppercase tracking-wide">
            Time range
          </label>
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500 w-10">Start</span>
              <input
                type="date"
                value={startParts.date}
                onChange={(e) => set("start", joinDt(e.target.value, startParts.time))}
                className="border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <div className="w-24">
                <ClockPicker
                  value={startParts.time}
                  onChange={(t) => set("start", joinDt(startParts.date, t))}
                />
              </div>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500 w-10">End</span>
              <input
                type="date"
                value={endParts.date}
                onChange={(e) => set("end", joinDt(e.target.value, endParts.time))}
                className="border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <div className="w-24">
                <ClockPicker
                  value={endParts.time}
                  onChange={(t) => set("end", joinDt(endParts.date, t))}
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Colors */}
      <div>
        <label className="block text-xs text-gray-500 mb-1.5 uppercase tracking-wide">
          Color
        </label>
        <div className="flex items-start gap-3 overflow-x-auto pb-1">
          {COLORS.map((c) => {
            const active = selectedColors.has(c);
            const bg = COLOR_MAP[c];
            const isLight = c === "white" || c === "silver" || c === "yellow" || c === "unknown";
            return (
              <button
                key={c}
                type="button"
                onClick={() => toggle("color", c)}
                title={c}
                aria-pressed={active}
                className="group"
              >
                <span
                  className={`relative inline-flex items-center justify-center rounded-full transition-all ${
                    active
                      ? "ring-2 ring-blue-600 ring-offset-2 scale-110"
                      : "group-hover:scale-105"
                  }`}
                  style={{
                    width: 30,
                    height: 30,
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
              </button>
            );
          })}
        </div>
      </div>

      {/* Two-column pictogram grids */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Object class */}
        <FilterTileGroup
          title="Object class"
          items={OBJECT_CLASSES}
          selected={selectedClasses}
          icons={OBJECT_CLASS_ICONS}
          counts={classCounts}
          onToggle={(v) => toggle("object_class", v)}
        />

        {/* Event type + Track state stacked */}
        <div className="space-y-4">
          <FilterTileGroup
            title="Event type"
            items={EVENT_TYPES}
            selected={selectedEvents}
            icons={EVENT_TYPE_ICONS}
            onToggle={(v) => toggle("event_type", v)}
          />
          <FilterTileGroup
            title="Track state"
            items={TRACK_STATES}
            selected={selectedStates}
            icons={TRACK_STATE_ICONS}
            onToggle={(v) => toggle("state", v)}
          />
        </div>
      </div>

      {/* Search + clear */}
      <div className="flex items-center gap-3 pt-1">
        <button
          onClick={onSearch}
          className="bg-blue-600 text-white rounded px-5 py-2 text-sm font-medium hover:bg-blue-700 transition-colors"
        >
          Search
        </button>
        <button
          onClick={() =>
            onChange({
              camera_id: "",
              start: "",
              end: "",
              object_class: "",
              color: "",
              event_type: "",
              state: "",
            })
          }
          className="text-sm text-gray-500 hover:text-gray-900 px-3 py-2"
        >
          Clear all
        </button>
        {onThumbOnlyChange && (
          <label className="flex items-center gap-1.5 text-sm text-gray-600 cursor-pointer ml-auto">
            <input
              type="checkbox"
              checked={!!thumbOnly}
              onChange={(e) => onThumbOnlyChange(e.target.checked)}
              className="rounded"
            />
            With thumbnails only
          </label>
        )}
      </div>
    </div>
  );
}

interface FilterTileGroupProps {
  title: string;
  items: string[];
  selected: Set<string>;
  icons: Record<string, (p: { className?: string }) => JSX.Element>;
  counts?: Record<string, number>;
  onToggle: (value: string) => void;
}

function FilterTileGroup({
  title,
  items,
  selected,
  icons,
  counts,
  onToggle,
}: FilterTileGroupProps) {
  return (
    <div className="border border-gray-200 rounded-lg p-3">
      <div className="text-[11px] font-semibold text-gray-600 uppercase tracking-wider mb-2">
        {title}
      </div>
      <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
        {items.map((v) => {
          const Icon = icons[v];
          const active = selected.has(v);
          return (
            <button
              key={v}
              type="button"
              onClick={() => onToggle(v)}
              aria-pressed={active}
              className={`group flex flex-col items-center justify-center gap-1 p-2 rounded border-2 transition ${
                active
                  ? "border-blue-600 bg-blue-50 text-blue-900"
                  : "border-gray-200 bg-gray-50 text-gray-500 hover:border-gray-400 hover:bg-white"
              }`}
            >
              {Icon ? (
                <Icon className={`w-8 h-8 ${active ? "text-blue-700" : "text-gray-500 group-hover:text-gray-800"}`} />
              ) : (
                <span className="w-8 h-8" />
              )}
              <span
                className={`text-[10px] leading-tight font-medium ${
                  active ? "text-blue-900" : "text-gray-600"
                }`}
              >
                {LABELS[v] ?? v}
                {counts && counts[v] != null && (
                  <span className="ml-1 text-gray-400 font-normal">
                    ({counts[v].toLocaleString()})
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
