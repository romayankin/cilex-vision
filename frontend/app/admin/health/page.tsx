"use client";

import { useCallback, useEffect, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface ConcurrencyStats {
  concurrent_now: number;
  concurrent_peak_5m: number;
  level: "ok" | "warning" | "critical";
  message: string | null;
  workers: number;
  warning_threshold: number;
  critical_threshold: number;
}

const GRAFANA_URL = process.env.NEXT_PUBLIC_GRAFANA_URL || "http://localhost:3001";

const PANELS = [
  { title: "Stream Health", path: "/d/stream-health" },
  { title: "Inference Performance", path: "/d/inference-performance" },
  { title: "Bus Health", path: "/d/bus-health" },
  { title: "Storage", path: "/d/storage" },
  { title: "Storage Tiering", path: "/d/storage-tiering" },
  { title: "Model Quality", path: "/d/model-quality" },
  { title: "MTMC Health", path: "/d/mtmc-health" },
];

export default function HealthPage() {
  const role = getUserRole();
  const [refreshKey, setRefreshKey] = useState(0);
  const [concurrency, setConcurrency] = useState<ConcurrencyStats | null>(null);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  useEffect(() => {
    fetch("/api/health/concurrency")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setConcurrency(d))
      .catch(() => {});
  }, [refreshKey]);

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

      {concurrency && (
        <div className="bg-white border border-gray-200 rounded-lg p-4 text-sm">
          <div className="flex items-center justify-between">
            <span className="text-gray-600">API Concurrency</span>
            <span
              className={`font-mono text-xs px-2 py-0.5 rounded ${
                concurrency.level === "ok"
                  ? "bg-green-100 text-green-700"
                  : concurrency.level === "warning"
                  ? "bg-amber-100 text-amber-700"
                  : "bg-red-100 text-red-700"
              }`}
            >
              {concurrency.concurrent_now} now / {concurrency.concurrent_peak_5m} peak (5m)
            </span>
          </div>
          {concurrency.message && (
            <p className="mt-2 text-xs text-gray-500">{concurrency.message}</p>
          )}
        </div>
      )}

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
