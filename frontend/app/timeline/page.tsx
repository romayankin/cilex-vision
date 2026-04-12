"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getTopologyGraph, type CameraNode } from "@/lib/api-client";

export default function TimelineIndexPage() {
  const [cameras, setCameras] = useState<CameraNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const topo = await getTopologyGraph("site-01");
        setCameras(topo.cameras ?? []);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Failed to load cameras";
        setError(msg.includes("401") ? "Login required" : msg);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) {
    return <div className="text-center py-8 text-gray-400">Loading cameras...</div>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Camera Timelines</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
          {error}
        </div>
      )}

      {cameras.length === 0 && !error && (
        <div className="text-center py-12 text-gray-400">
          No cameras configured. Add cameras via the Admin panel.
        </div>
      )}

      {cameras.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {cameras.map((cam) => (
            <Link
              key={cam.camera_id}
              href={`/timeline/${cam.camera_id}`}
              className="block bg-white border border-gray-200 rounded-lg p-4 hover:border-blue-300 hover:shadow-sm transition"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="font-medium text-sm">{cam.name || cam.camera_id}</span>
                <span
                  className={`text-xs px-1.5 py-0.5 rounded ${
                    cam.status === "online"
                      ? "bg-green-100 text-green-700"
                      : "bg-gray-100 text-gray-500"
                  }`}
                >
                  {cam.status}
                </span>
              </div>
              <p className="text-xs text-gray-400">{cam.camera_id}</p>
              {cam.location_description && (
                <p className="text-xs text-gray-400 mt-1">{cam.location_description}</p>
              )}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
