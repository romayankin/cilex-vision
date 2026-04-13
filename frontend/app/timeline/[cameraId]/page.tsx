"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import VideoPlayer from "@/components/VideoPlayer";
import Timeline, { TimelineEntry } from "@/components/Timeline";
import { getDetections, getEvents, getTrackDetail } from "@/lib/api-client";
import { getStreamUrls } from "@/lib/stream-urls";

const POLL_INTERVAL = 5000;

export default function CameraTimelinePage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const cameraId = params.cameraId as string;
  const highlightTrack = searchParams.get("track");

  const [entries, setEntries] = useState<TimelineEntry[]>([]);
  const [clipUrl, setClipUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [trackDetail, setTrackDetail] = useState<{
    objectClass: string;
    state: string;
    attributes: { attribute_type: string; color_value: string }[];
  } | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const now = new Date().toISOString();
      const thirtyMinAgo = new Date(Date.now() - 30 * 60_000).toISOString();

      const [detRes, evtRes] = await Promise.all([
        getDetections({
          camera_id: cameraId,
          start: thirtyMinAgo,
          end: now,
          limit: 100,
        }),
        getEvents({
          camera_id: cameraId,
          start: thirtyMinAgo,
          end: now,
          limit: 100,
        }),
      ]);

      const items: TimelineEntry[] = [];

      for (const det of detRes.detections) {
        items.push({
          id: `det-${det.camera_id}-${det.frame_seq}`,
          timestamp: det.time,
          type: "detection",
          objectClass: det.object_class,
          cameraId: det.camera_id,
          confidence: det.confidence,
          trackId: det.local_track_id,
        });
      }

      for (const ev of evtRes.events) {
        items.push({
          id: `evt-${ev.event_id}`,
          timestamp: ev.start_time,
          type: "event",
          eventType: ev.event_type,
          cameraId: ev.camera_id,
          trackId: ev.track_id,
          clipUrl: ev.clip_url,
        });

        // Use first available clip for the player
        if (ev.clip_url && !clipUrl) {
          setClipUrl(ev.clip_url);
        }
      }

      // Sort by time descending (newest first)
      items.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

      setEntries(items);

      // Load highlighted track detail if specified
      if (highlightTrack && !trackDetail) {
        try {
          const detail = await getTrackDetail(highlightTrack);
          setTrackDetail({
            objectClass: detail.object_class,
            state: detail.state,
            attributes: detail.attributes.map((a) => ({
              attribute_type: a.attribute_type,
              color_value: a.color_value,
            })),
          });
        } catch {
          /* track detail may not exist */
        }
      }
    } catch {
      /* polling failure is non-fatal */
    } finally {
      setLoading(false);
    }
  }, [cameraId, clipUrl, highlightTrack, trackDetail]);

  // Initial fetch + polling
  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, POLL_INTERVAL);
    return () => clearInterval(interval);
  }, [fetchData]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">
          Timeline: {cameraId}
        </h1>
        <span className="text-xs text-gray-400">Auto-refreshing every 5s</span>
      </div>

      {/* Track detail card */}
      {trackDetail && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3">
          <div className="flex items-center gap-3 text-sm">
            <span className="font-medium">Track: {highlightTrack?.slice(0, 8)}...</span>
            <span className="px-2 py-0.5 rounded bg-blue-100 text-blue-800 text-xs">
              {trackDetail.objectClass}
            </span>
            <span className="px-2 py-0.5 rounded bg-gray-100 text-gray-600 text-xs">
              {trackDetail.state}
            </span>
            {trackDetail.attributes.map((a, i) => (
              <span key={i} className="text-xs text-gray-600">
                {a.attribute_type}: {a.color_value}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Video player — falls back to the live feed when no event clip is available */}
      {clipUrl ? (
        <VideoPlayer src={clipUrl} autoPlay />
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-gray-400">
            No recorded events yet. Showing live feed.
          </p>
          <video
            src={getStreamUrls(cameraId).mse_url}
            autoPlay
            muted
            playsInline
            className="w-full rounded-lg bg-black aspect-video object-cover"
          />
        </div>
      )}

      {/* Timeline entries */}
      {loading ? (
        <div className="text-center py-8 text-gray-400">Loading timeline...</div>
      ) : (
        <Timeline entries={entries} />
      )}
    </div>
  );
}
