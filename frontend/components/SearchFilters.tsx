"use client";

import { useCallback } from "react";

const OBJECT_CLASSES = ["person", "car", "truck", "bus", "bicycle", "motorcycle", "animal"];
const COLORS = ["red", "blue", "white", "black", "silver", "green", "yellow", "brown", "orange", "unknown"];
const EVENT_TYPES = ["entered_scene", "exited_scene", "stopped", "loitering", "motion_started", "motion_ended"];
const TRACK_STATES = ["new", "active", "lost", "terminated"];

export interface FilterState {
  camera_id: string;
  start: string;
  end: string;
  object_class: string;
  color: string;
  event_type: string;
  state: string;
}

interface SearchFiltersProps {
  filters: FilterState;
  onChange: (filters: FilterState) => void;
  onSearch: () => void;
}

export default function SearchFilters({ filters, onChange, onSearch }: SearchFiltersProps) {
  const set = useCallback(
    (key: keyof FilterState, value: string) => {
      onChange({ ...filters, [key]: value });
    },
    [filters, onChange],
  );

  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4 space-y-4">
      <h2 className="font-semibold text-sm text-gray-700 uppercase tracking-wide">Filters</h2>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Camera ID</label>
          <input
            type="text"
            value={filters.camera_id}
            onChange={(e) => set("camera_id", e.target.value)}
            placeholder="e.g. cam-01"
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">Start</label>
          <input
            type="datetime-local"
            value={filters.start}
            onChange={(e) => set("start", e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">End</label>
          <input
            type="datetime-local"
            value={filters.end}
            onChange={(e) => set("end", e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">Object Class</label>
          <select
            value={filters.object_class}
            onChange={(e) => set("object_class", e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">All</option>
            {OBJECT_CLASSES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">Color</label>
          <select
            value={filters.color}
            onChange={(e) => set("color", e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">All</option>
            {COLORS.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">Event Type</label>
          <select
            value={filters.event_type}
            onChange={(e) => set("event_type", e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">All</option>
            {EVENT_TYPES.map((t) => (
              <option key={t} value={t}>{t.replace(/_/g, " ")}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">Track State</label>
          <select
            value={filters.state}
            onChange={(e) => set("state", e.target.value)}
            className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm bg-white focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">All</option>
            {TRACK_STATES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>

        <div className="flex items-end">
          <button
            onClick={onSearch}
            className="w-full bg-blue-600 text-white rounded px-4 py-1.5 text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            Search
          </button>
        </div>
      </div>
    </div>
  );
}
