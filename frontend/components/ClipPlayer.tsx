"use client";

import { useMemo } from "react";

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
    return {
      url: rangeEndpoint,
      unavailableReason: "Range playback ships in Phase 9 — endpoint not deployed yet",
    };
  }

  return { url: null, unavailableReason: `Unknown source type: ${sourceType ?? "null"}` };
}

export default function ClipPlayer({ uri, sourceType, className }: ClipPlayerProps) {
  const resolved = useMemo(() => resolveUri(uri, sourceType), [uri, sourceType]);
  const showPhase9Notice = sourceType === "segment_range";

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
      {showPhase9Notice && (
        <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1 mb-1">
          Range playback endpoint ships in Phase 9. If this fails to load, that is expected.
        </div>
      )}
      <video src={resolved.url} controls autoPlay={false} className="w-full rounded" />
    </div>
  );
}
