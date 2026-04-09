"use client";

import Link from "next/link";

const EVENT_BADGE: Record<string, string> = {
  entered_scene: "bg-green-100 text-green-800",
  exited_scene: "bg-red-100 text-red-800",
  stopped: "bg-yellow-100 text-yellow-800",
  loitering: "bg-orange-100 text-orange-800",
  motion_started: "bg-blue-100 text-blue-800",
  motion_ended: "bg-gray-100 text-gray-800",
};

export interface TimelineEntry {
  id: string;
  timestamp: string;
  type: "detection" | "event";
  objectClass?: string;
  eventType?: string;
  cameraId: string;
  confidence?: number;
  trackId?: string | null;
  clipUrl?: string | null;
}

interface TimelineProps {
  entries: TimelineEntry[];
}

export default function Timeline({ entries }: TimelineProps) {
  if (entries.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400">
        No events in this time range.
      </div>
    );
  }

  return (
    <div className="relative">
      {/* Vertical line */}
      <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-gray-200" />

      <div className="space-y-4">
        {entries.map((entry) => {
          const ts = new Date(entry.timestamp);
          const badge =
            entry.type === "event" && entry.eventType
              ? EVENT_BADGE[entry.eventType] ?? "bg-gray-100 text-gray-800"
              : "bg-blue-50 text-blue-700";

          return (
            <div key={entry.id} className="relative pl-10">
              {/* Dot */}
              <div className="absolute left-2.5 top-2 w-3 h-3 rounded-full bg-white border-2 border-blue-500" />

              <div className="bg-white border border-gray-200 rounded-lg p-3">
                <div className="flex items-center justify-between mb-1">
                  <div className="flex items-center gap-2">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${badge}`}>
                      {entry.type === "event" ? entry.eventType?.replace(/_/g, " ") : "detection"}
                    </span>
                    {entry.objectClass && (
                      <span className="text-xs text-gray-600">{entry.objectClass}</span>
                    )}
                  </div>
                  <span className="text-xs text-gray-400">{entry.cameraId}</span>
                </div>

                <div className="flex items-center justify-between text-xs text-gray-500">
                  <span>{ts.toLocaleString()}</span>
                  <div className="flex items-center gap-2">
                    {entry.confidence !== undefined && (
                      <span>{(entry.confidence * 100).toFixed(0)}%</span>
                    )}
                    {entry.trackId && (
                      <Link
                        href={`/timeline/${entry.cameraId}?track=${entry.trackId}`}
                        className="text-blue-600 hover:underline"
                      >
                        Track
                      </Link>
                    )}
                    {entry.clipUrl && (
                      <a href={entry.clipUrl} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
                        Clip
                      </a>
                    )}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
