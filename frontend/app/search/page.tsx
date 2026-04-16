"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import FilterSidebar, { FilterState } from "@/components/FilterSidebar";
import ResultCard from "@/components/ResultCard";
import {
  getDetections,
  getEvents,
  getTracks,
  getTrackDetail,
  type TrackDetailResponse,
} from "@/lib/api-client";

const PAGE_SIZE = 20;
const FILTER_DEBOUNCE_MS = 300;

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

function filtersEqual(a: FilterState, b: FilterState): boolean {
  return (
    a.camera_id === b.camera_id &&
    a.start === b.start &&
    a.end === b.end &&
    a.object_class === b.object_class &&
    a.color === b.color &&
    a.event_type === b.event_type &&
    a.state === b.state
  );
}

export default function SearchPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [filters, setFilters] = useState<FilterState>(() =>
    parseFiltersFromParams(searchParams),
  );
  const [debouncedFilters, setDebouncedFilters] = useState<FilterState>(filters);
  const [results, setResults] = useState<ResultItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [thumbOnly, setThumbOnly] = useState(true);
  const requestIdRef = useRef(0);

  const [nlpQuery, setNlpQuery] = useState("");
  const [nlpLoading, setNlpLoading] = useState(false);
  const [nlpExplanation, setNlpExplanation] = useState<string | null>(null);
  const [nlpError, setNlpError] = useState<string | null>(null);
  const [nlpAvailable, setNlpAvailable] = useState<boolean | null>(null);

  useEffect(() => {
    fetch("/api/search/nlp/status", { credentials: "include" })
      .then((r) => r.json())
      .then((d) => setNlpAvailable(d.available === true))
      .catch(() => setNlpAvailable(false));
  }, []);

  async function handleNlpSearch() {
    if (!nlpQuery.trim() || !nlpAvailable) return;
    setNlpLoading(true);
    setNlpError(null);
    setNlpExplanation(null);

    try {
      const res = await fetch("/api/search/nlp", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: nlpQuery }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setNlpError(body.detail || `HTTP ${res.status}`);
        return;
      }

      const data = await res.json();

      if (data.parse_error) {
        setNlpError(data.explanation);
        return;
      }

      if (data.filters && Object.keys(data.filters).length > 0) {
        setFilters((prev) => ({
          ...prev,
          camera_id: data.filters.camera_id || prev.camera_id,
          object_class: data.filters.object_class || prev.object_class,
          event_type: data.filters.event_type || prev.event_type,
          start: data.filters.start || prev.start,
          end: data.filters.end || prev.end,
          color: data.filters.color || prev.color,
          state: data.filters.state || prev.state,
        }));
      }

      if (data.explanation) {
        setNlpExplanation(data.explanation);
      }
    } catch (err) {
      setNlpError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setNlpLoading(false);
    }
  }

  useEffect(() => {
    const id = window.setTimeout(() => {
      setDebouncedFilters((prev) => (filtersEqual(prev, filters) ? prev : filters));
    }, FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [filters]);

  const doSearch = useCallback(
    async (currentFilters: FilterState, currentThumbOnly: boolean, newOffset: number) => {
      setLoading(true);
      setError(null);

      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(currentFilters)) {
        if (v) params.set(k, v);
      }
      if (newOffset > 0) params.set("offset", String(newOffset));
      router.replace(`/search?${params.toString()}`, { scroll: false });

      const reqId = ++requestIdRef.current;

      try {
        const items: ResultItem[] = [];
        let fetchedTotal = 0;

        if (currentFilters.event_type) {
          const res = await getEvents({
            camera_id: currentFilters.camera_id || undefined,
            start: currentFilters.start || undefined,
            end: currentFilters.end || undefined,
            event_type: currentFilters.event_type || undefined,
            state: currentFilters.state || undefined,
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
        } else if (currentFilters.state) {
          const res = await getTracks({
            camera_id: currentFilters.camera_id || undefined,
            start: currentFilters.start || undefined,
            end: currentFilters.end || undefined,
            object_class: currentFilters.object_class || undefined,
            state: currentFilters.state || undefined,
            offset: newOffset,
            limit: PAGE_SIZE,
          });
          fetchedTotal = res.total;
          for (const tr of res.tracks) {
            let detail: TrackDetailResponse | null = null;
            try {
              detail = await getTrackDetail(tr.local_track_id);
            } catch {
              /* optional */
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
        } else {
          const res = await getDetections({
            camera_id: currentFilters.camera_id || undefined,
            start: currentFilters.start || undefined,
            end: currentFilters.end || undefined,
            object_class: currentFilters.object_class || undefined,
            has_thumbnail: currentThumbOnly || undefined,
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
              thumbnailUrl: det.thumbnail_url,
            });
          }
        }

        if (reqId !== requestIdRef.current) return;

        setResults(items);
        setTotal(fetchedTotal);
        setOffset(newOffset);
      } catch (err) {
        if (reqId !== requestIdRef.current) return;
        setError(err instanceof Error ? err.message : "Search failed");
      } finally {
        if (reqId === requestIdRef.current) setLoading(false);
      }
    },
    [router],
  );

  useEffect(() => {
    doSearch(debouncedFilters, thumbOnly, 0);
  }, [debouncedFilters, thumbOnly, doSearch]);

  const clearAll = () =>
    setFilters({
      camera_id: "",
      start: "",
      end: "",
      object_class: "",
      color: "",
      event_type: "",
      state: "",
    });

  const activeCount = Object.values(filters).filter(Boolean).length;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Search</h1>
        <div className="flex items-center gap-3 text-sm text-gray-500">
          {loading ? (
            <span className="text-gray-400">Searching…</span>
          ) : (
            <span>
              {total.toLocaleString()} result{total === 1 ? "" : "s"}
            </span>
          )}
          {activeCount > 0 && (
            <button
              type="button"
              onClick={clearAll}
              className="text-xs text-gray-500 hover:text-gray-900 border border-gray-200 rounded px-2 py-1"
            >
              Clear {activeCount} filter{activeCount === 1 ? "" : "s"}
            </button>
          )}
        </div>
      </div>

      <div className="flex gap-2">
        <div className="flex-1 relative">
          <input
            type="text"
            value={nlpQuery}
            onChange={(e) => setNlpQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleNlpSearch()}
            placeholder={
              nlpAvailable === false
                ? "AI search unavailable — Ollama not running"
                : nlpAvailable === null
                  ? "Checking AI search availability..."
                  : 'Try: "person entering server room Monday morning"'
            }
            className="w-full border border-gray-300 rounded-lg px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none disabled:bg-gray-100 disabled:text-gray-500"
            disabled={nlpLoading || nlpAvailable === false}
          />
          {nlpLoading && (
            <div className="absolute right-3 top-1/2 -translate-y-1/2">
              <div className="w-4 h-4 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={handleNlpSearch}
          disabled={nlpLoading || !nlpQuery.trim() || nlpAvailable !== true}
          className="px-4 py-2.5 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed whitespace-nowrap"
        >
          {nlpLoading ? "Thinking..." : "AI Search"}
        </button>
      </div>

      {nlpExplanation && (
        <div className="flex items-start gap-2 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2 text-sm text-blue-800">
          <span className="text-blue-500 mt-0.5 flex-shrink-0">✨</span>
          <div>
            <span className="font-medium">AI understood:</span> {nlpExplanation}
          </div>
          <button
            type="button"
            onClick={() => setNlpExplanation(null)}
            className="ml-auto text-blue-400 hover:text-blue-600 text-xs flex-shrink-0"
          >
            ✕
          </button>
        </div>
      )}

      {nlpError && (
        <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-sm text-amber-800">
          <span className="flex-shrink-0">⚠</span>
          <div>{nlpError}</div>
          <button
            type="button"
            onClick={() => setNlpError(null)}
            className="ml-auto text-amber-400 hover:text-amber-600 text-xs flex-shrink-0"
          >
            ✕
          </button>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {!loading && results.length > 0 && (
        <>
          <div className="text-xs text-gray-500">
            Showing {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} of{" "}
            {total.toLocaleString()} results
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
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

          <div className="flex items-center justify-center gap-4 pt-4">
            <button
              onClick={() =>
                doSearch(debouncedFilters, thumbOnly, Math.max(0, offset - PAGE_SIZE))
              }
              disabled={offset === 0}
              className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Previous
            </button>
            <button
              onClick={() => doSearch(debouncedFilters, thumbOnly, offset + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= total}
              className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </>
      )}

      {!loading && results.length === 0 && !error && (
        <div className="text-center py-12 text-gray-400">
          No results match the current filters.
        </div>
      )}

      <FilterSidebar
        filters={filters}
        onChange={setFilters}
        thumbOnly={thumbOnly}
        onThumbOnlyChange={setThumbOnly}
      />
    </div>
  );
}
