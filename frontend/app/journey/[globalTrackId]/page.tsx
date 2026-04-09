"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import JourneyMap, { JourneyStop } from "@/components/JourneyMap";
import { getTracks, getTrackDetail } from "@/lib/api-client";

export default function JourneyPage() {
  const params = useParams();
  const globalTrackId = params.globalTrackId as string;
  const [stops, setStops] = useState<JourneyStop[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        // In a full implementation, this would call a dedicated
        // global_track API endpoint.  For now, we demonstrate the
        // journey view with track data available through the
        // existing Query API.
        const res = await getTracks({ limit: 50 });

        const journeyStops: JourneyStop[] = [];

        for (const tr of res.items.slice(0, 10)) {
          let attrs: { color_value: string; attribute_type: string }[] = [];
          let thumb: string | null = null;
          try {
            const detail = await getTrackDetail(tr.local_track_id);
            attrs = detail.attributes.map((a) => ({
              color_value: a.color_value,
              attribute_type: a.attribute_type,
            }));
            thumb = detail.thumbnail_url;
          } catch {
            /* detail may not exist */
          }

          journeyStops.push({
            cameraId: tr.camera_id,
            entryTime: tr.start_time,
            exitTime: tr.end_time,
            durationMs: tr.end_time
              ? new Date(tr.end_time).getTime() - new Date(tr.start_time).getTime()
              : null,
            attributes: attrs,
            thumbnailUrl: thumb,
          });
        }

        // Sort by entry time
        journeyStops.sort(
          (a, b) => new Date(a.entryTime).getTime() - new Date(b.entryTime).getTime(),
        );

        setStops(journeyStops);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load journey");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [globalTrackId]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Cross-Camera Journey</h1>
        <p className="text-sm text-gray-500 mt-1">
          Global Track: {globalTrackId}
        </p>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-center py-8 text-gray-400">Loading journey...</div>
      ) : (
        <JourneyMap stops={stops} globalTrackId={globalTrackId} />
      )}
    </div>
  );
}
