"use client";

import { useState, useCallback } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

const GRAFANA_URL = process.env.NEXT_PUBLIC_GRAFANA_URL || "http://localhost:3001";

const PANELS = [
  { title: "Stream Health", path: "/d/stream-health" },
  { title: "Inference Performance", path: "/d/inference-perf" },
  { title: "Bus Health", path: "/d/bus-health" },
  { title: "Storage", path: "/d/storage" },
  { title: "Model Quality", path: "/d/model-quality" },
  { title: "Node Exporter", path: "/d/node-exporter" },
];

export default function HealthPage() {
  const role = getUserRole();
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">System Health</h1>
        <button
          onClick={refresh}
          className="px-3 py-1.5 bg-gray-100 text-gray-700 text-sm rounded hover:bg-gray-200"
        >
          Refresh
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {PANELS.map((panel) => (
          <div key={panel.path} className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <div className="px-3 py-2 border-b border-gray-100 bg-gray-50">
              <h3 className="text-sm font-medium">{panel.title}</h3>
            </div>
            {/* eslint-disable-next-line jsx-a11y/iframe-has-title */}
            <iframe
              key={`${panel.path}-${refreshKey}`}
              src={`${GRAFANA_URL}${panel.path}?orgId=1&kiosk`}
              width="100%"
              height="300"
              className="border-0"
            />
          </div>
        ))}
      </div>
    </div>
  );
}
