"use client";

import { useMemo, useState } from "react";

interface ClipPlayerProps {
  uri: string | null;
  sourceType: "standalone" | "segment_range" | string | null;
  className?: string;
}

interface Resolved {
  url: string | null;
  unavailableReason: string | null;
}

function resolveUri(uri: string | null, sourceType: ClipPlayerProps["sourceType"]): Resolved {
  if (!uri) return { url: null, unavailableReason: "No clip available" };

  if (sourceType === "standalone") {
    const key = uri.replace(/^s3:\/\/event-clips\//, "");
    if (!key || key === uri) {
      return { url: null, unavailableReason: "Invalid standalone URI" };
    }
    const encoded = key.split("/").map(encodeURIComponent).join("/");
    return { url: `/api/clips/s3/${encoded}`, unavailableReason: null };
  }

  if (sourceType === "segment_range") {
    // range:<camera_id>:<start_iso>|<end_iso>
    const match = uri.match(/^range:([^:]+):(.+)\|(.+)$/);
    if (!match) return { url: null, unavailableReason: "Invalid range URI" };
    const [, cameraId, start, end] = match;
    const rangeEndpoint =
      `/api/clips/range?camera_id=${encodeURIComponent(cameraId)}` +
      `&start=${encodeURIComponent(start)}` +
      `&end=${encodeURIComponent(end)}`;
    return { url: rangeEndpoint, unavailableReason: null };
  }

  return { url: null, unavailableReason: `Unknown source type: ${sourceType ?? "null"}` };
}

export default function ClipPlayer({ uri, sourceType, className }: ClipPlayerProps) {
  const resolved = useMemo(() => resolveUri(uri, sourceType), [uri, sourceType]);
  const [loadError, setLoadError] = useState<string | null>(null);

  if (!resolved.url) {
    return (
      <div
        className={`bg-gray-100 border border-gray-200 rounded p-4 text-center text-sm text-gray-500 ${className ?? ""}`}
      >
        {resolved.unavailableReason ?? "Clip unavailable"}
      </div>
    );
  }

  return (
    <div className={className}>
      <video
        src={resolved.url}
        controls
        autoPlay={false}
        onError={(e) => {
          const videoEl = e.currentTarget;
          setLoadError(
            videoEl.error?.code === 4
              ? "Clip format not supported or corrupted"
              : "Failed to load clip",
          );
        }}
        className="w-full rounded"
      />
      {loadError && (
        <div className="mt-1 text-xs text-red-700 bg-red-50 border border-red-200 rounded px-2 py-1">
          {loadError}
        </div>
      )}
    </div>
  );
}
