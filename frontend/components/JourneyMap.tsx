"use client";

import Link from "next/link";

export interface JourneyStop {
  cameraId: string;
  cameraName?: string;
  entryTime: string;
  exitTime: string | null;
  durationMs: number | null;
  attributes: { color_value: string; attribute_type: string }[];
  thumbnailUrl?: string | null;
}

interface JourneyMapProps {
  stops: JourneyStop[];
  globalTrackId: string;
}

export default function JourneyMap({ stops, globalTrackId }: JourneyMapProps) {
  if (stops.length === 0) {
    return (
      <div className="text-center py-8 text-gray-400">
        No camera visits found for this journey.
      </div>
    );
  }

  return (
    <div className="space-y-0">
      {stops.map((stop, idx) => {
        const entry = new Date(stop.entryTime);
        const exit = stop.exitTime ? new Date(stop.exitTime) : null;
        const dur = stop.durationMs
          ? `${(stop.durationMs / 1000).toFixed(1)}s`
          : exit
            ? `${((exit.getTime() - entry.getTime()) / 1000).toFixed(1)}s`
            : "ongoing";

        return (
          <div key={stop.cameraId + idx}>
            {/* Camera node */}
            <div className="flex items-start gap-4">
              {/* Visual connector */}
              <div className="flex flex-col items-center">
                <div className="w-8 h-8 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs font-bold">
                  {idx + 1}
                </div>
                {idx < stops.length - 1 && (
                  <div className="w-0.5 h-12 bg-blue-300 mt-1" />
                )}
              </div>

              {/* Content */}
              <div className="flex-1 bg-white border border-gray-200 rounded-lg p-3 mb-2">
                <div className="flex items-center justify-between mb-2">
                  <div>
                    <span className="font-medium text-sm">{stop.cameraName ?? stop.cameraId}</span>
                    <span className="ml-2 text-xs text-gray-400">{stop.cameraId}</span>
                  </div>
                  <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded">
                    {dur}
                  </span>
                </div>

                <div className="flex items-center gap-4 text-xs text-gray-500">
                  <span>In: {entry.toLocaleTimeString()}</span>
                  <span>Out: {exit ? exit.toLocaleTimeString() : "---"}</span>
                </div>

                {stop.attributes.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-2">
                    {stop.attributes.map((a, ai) => (
                      <span key={ai} className="px-1.5 py-0.5 rounded bg-gray-50 text-xs text-gray-700">
                        {a.attribute_type.replace(/_/g, " ")}: {a.color_value}
                      </span>
                    ))}
                  </div>
                )}

                {/* Thumbnail or placeholder */}
                <div className="mt-2 h-16 bg-gray-100 rounded flex items-center justify-center">
                  {stop.thumbnailUrl ? (
                    <img src={stop.thumbnailUrl} alt="" className="h-full object-cover rounded" />
                  ) : (
                    <span className="text-xs text-gray-400">No thumbnail</span>
                  )}
                </div>

                <Link
                  href={`/timeline/${stop.cameraId}`}
                  className="inline-block mt-2 text-xs text-blue-600 hover:underline"
                >
                  View camera timeline
                </Link>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
