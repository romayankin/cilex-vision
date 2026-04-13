"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import SearchFilters, { FilterState } from "@/components/SearchFilters";
import ResultCard from "@/components/ResultCard";
import {
  getDetections,
  getEvents,
  getTracks,
  getTrackDetail,
  type TrackDetailResponse,
  type DetectionResponse,
  type EventResponse,
} from "@/lib/api-client";

const PAGE_SIZE = 20;

type ResultItem = {
  kind: "detection" | "event" | "track";
  id: string;
  trackId: string | null;
  cameraId: string;
  objectClass: string;
  timestamp: string;
  confidence: number;
  thumbnailUrl?: string | null;
  clipUrl?: string | null;
  attributes?: { attribute_type: string; color_value: string; confidence: number }[];
};

function parseFiltersFromParams(params: URLSearchParams): FilterState {
  return {
    camera_id: params.get("camera_id") ?? "",
    start: params.get("start") ?? "",
    end: params.get("end") ?? "",
    object_class: params.get("object_class") ?? "",
    color: params.get("color") ?? "",
    event_type: params.get("event_type") ?? "",
    state: params.get("state") ?? "",
  };
}

export default function SearchPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [filters, setFilters] = useState<FilterState>(() => parseFiltersFromParams(searchParams));
  const [results, setResults] = useState<ResultItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const doSearch = useCallback(
    async (newOffset = 0) => {
      setLoading(true);
      setError(null);

      // Sync URL
      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(filters)) {
        if (v) params.set(k, v);
      }
      if (newOffset > 0) params.set("offset", String(newOffset));
      router.replace(`/search?${params.toString()}`, { scroll: false });

      try {
        const items: ResultItem[] = [];
        let fetchedTotal = 0;

        // If event_type filter is set, search events
        if (filters.event_type) {
          const res = await getEvents({
            camera_id: filters.camera_id || undefined,
            start: filters.start || undefined,
            end: filters.end || undefined,
            event_type: filters.event_type || undefined,
            state: filters.state || undefined,
            offset: newOffset,
            limit: PAGE_SIZE,
          });
          fetchedTotal = res.total;
          for (const ev of res.events) {
            items.push({
              kind: "event",
              id: ev.event_id,
              trackId: ev.track_id,
              cameraId: ev.camera_id,
              objectClass: ev.event_type,
              timestamp: ev.start_time,
              confidence: 1.0,
              clipUrl: ev.clip_url,
            });
          }
        }
        // If state filter is set, search tracks
        else if (filters.state) {
          const res = await getTracks({
            camera_id: filters.camera_id || undefined,
            start: filters.start || undefined,
            end: filters.end || undefined,
            object_class: filters.object_class || undefined,
            state: filters.state || undefined,
            offset: newOffset,
            limit: PAGE_SIZE,
          });
          fetchedTotal = res.total;
          for (const tr of res.tracks) {
            let detail: TrackDetailResponse | null = null;
            try {
              detail = await getTrackDetail(tr.local_track_id);
            } catch {
              /* track detail may fail */
            }
            items.push({
              kind: "track",
              id: tr.local_track_id,
              trackId: tr.local_track_id,
              cameraId: tr.camera_id,
              objectClass: tr.object_class,
              timestamp: tr.start_time,
              confidence: tr.mean_confidence ?? 0,
              thumbnailUrl: detail?.thumbnail_url,
              attributes: detail?.attributes,
            });
          }
        }
        // Default: search detections
        else {
          const res = await getDetections({
            camera_id: filters.camera_id || undefined,
            start: filters.start || undefined,
            end: filters.end || undefined,
            object_class: filters.object_class || undefined,
            offset: newOffset,
            limit: PAGE_SIZE,
          });
          fetchedTotal = res.total;
          for (const det of res.detections) {
            items.push({
              kind: "detection",
              id: `${det.camera_id}-${det.frame_seq}-${det.time}`,
              trackId: det.local_track_id,
              cameraId: det.camera_id,
              objectClass: det.object_class,
              timestamp: det.time,
              confidence: det.confidence,
            });
          }
        }

        setResults(items);
        setTotal(fetchedTotal);
        setOffset(newOffset);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Search failed");
      } finally {
        setLoading(false);
      }
    },
    [filters, router],
  );

  // Auto-search on mount if URL has params
  useEffect(() => {
    const hasParams = Array.from(searchParams.entries()).some(([k]) => k !== "offset");
    if (hasParams) {
      doSearch(Number(searchParams.get("offset") ?? 0));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Search</h1>

      <SearchFilters filters={filters} onChange={setFilters} onSearch={() => doSearch(0)} />

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {loading && (
        <div className="text-center py-8 text-gray-400">Loading...</div>
      )}

      {!loading && results.length > 0 && (
        <>
          <div className="text-sm text-gray-500">
            Showing {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} of {total} results
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {results.map((r) => (
              <ResultCard
                key={r.id}
                trackId={r.trackId}
                cameraId={r.cameraId}
                objectClass={r.objectClass}
                timestamp={r.timestamp}
                confidence={r.confidence}
                thumbnailUrl={r.thumbnailUrl}
                clipUrl={r.clipUrl}
                attributes={r.attributes}
              />
            ))}
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-center gap-4 pt-4">
            <button
              onClick={() => doSearch(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
              className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            <button
              onClick={() => doSearch(offset + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= total}
              className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </>
      )}

      {!loading && results.length === 0 && !error && total === 0 && (
        <div className="text-center py-12 text-gray-400">
          Use the filters above to search detections, tracks, and events.
        </div>
      )}
    </div>
  );
}
