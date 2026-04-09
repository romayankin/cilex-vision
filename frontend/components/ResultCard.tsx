"use client";

import Link from "next/link";

const CLASS_COLORS: Record<string, string> = {
  person: "bg-blue-100 text-blue-800",
  car: "bg-green-100 text-green-800",
  truck: "bg-yellow-100 text-yellow-800",
  bus: "bg-orange-100 text-orange-800",
  bicycle: "bg-purple-100 text-purple-800",
  motorcycle: "bg-red-100 text-red-800",
  animal: "bg-pink-100 text-pink-800",
};

const COLOR_DOT: Record<string, string> = {
  red: "bg-red-500",
  blue: "bg-blue-500",
  white: "bg-white border border-gray-300",
  black: "bg-gray-900",
  silver: "bg-gray-400",
  green: "bg-green-500",
  yellow: "bg-yellow-400",
  brown: "bg-amber-700",
  orange: "bg-orange-500",
  unknown: "bg-gray-300",
};

interface ResultCardProps {
  trackId: string | null;
  cameraId: string;
  objectClass: string;
  timestamp: string;
  confidence: number;
  thumbnailUrl?: string | null;
  clipUrl?: string | null;
  attributes?: { attribute_type: string; color_value: string; confidence: number }[];
}

export default function ResultCard({
  trackId,
  cameraId,
  objectClass,
  timestamp,
  confidence,
  thumbnailUrl,
  clipUrl,
  attributes,
}: ResultCardProps) {
  const badgeClass = CLASS_COLORS[objectClass] ?? "bg-gray-100 text-gray-800";
  const ts = new Date(timestamp);
  const timeStr = ts.toLocaleString();

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden hover:shadow-md transition-shadow">
      {/* Thumbnail or placeholder */}
      <div className="h-32 bg-gray-100 flex items-center justify-center">
        {thumbnailUrl ? (
          <img src={thumbnailUrl} alt="Thumbnail" className="h-full w-full object-cover" />
        ) : (
          <span className="text-gray-400 text-sm">No thumbnail</span>
        )}
      </div>

      <div className="p-3 space-y-2">
        {/* Class badge + camera */}
        <div className="flex items-center justify-between">
          <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${badgeClass}`}>
            {objectClass}
          </span>
          <span className="text-xs text-gray-500">{cameraId}</span>
        </div>

        {/* Timestamp + confidence */}
        <div className="flex items-center justify-between text-xs text-gray-600">
          <span>{timeStr}</span>
          <span>{(confidence * 100).toFixed(0)}%</span>
        </div>

        {/* Color attributes */}
        {attributes && attributes.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {attributes.map((a, i) => (
              <span key={i} className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-gray-50 text-xs text-gray-700">
                <span className={`inline-block w-2.5 h-2.5 rounded-full ${COLOR_DOT[a.color_value] ?? "bg-gray-300"}`} />
                {a.color_value}
              </span>
            ))}
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center gap-2 pt-1">
          {trackId && (
            <Link
              href={`/timeline/${cameraId}?track=${trackId}`}
              className="text-xs text-blue-600 hover:underline"
            >
              View timeline
            </Link>
          )}
          {clipUrl ? (
            <a
              href={clipUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-blue-600 hover:underline"
            >
              View clip
            </a>
          ) : (
            <span className="text-xs text-gray-400">No clip</span>
          )}
        </div>
      </div>
    </div>
  );
}
