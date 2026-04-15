"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

const GRAFANA_URL = process.env.NEXT_PUBLIC_GRAFANA_URL || "http://localhost:3001";

const DASHBOARDS = [
  { name: "Stream Health", path: "/d/stream-health", desc: "Camera streams, frame rates, connection status" },
  { name: "Inference Performance", path: "/d/inference-perf", desc: "Detection latency, throughput, GPU utilization" },
  { name: "Bus Health", path: "/d/bus-health", desc: "Kafka topics, consumer lag, NATS metrics" },
  { name: "Storage", path: "/d/storage", desc: "MinIO usage, TimescaleDB compression, retention" },
  { name: "Model Quality", path: "/d/model-quality", desc: "Detection accuracy, Re-ID match rates, attribute confidence" },
];

const ADMIN_SECTIONS = [
  { href: "/admin/cameras", name: "Cameras", desc: "Add, edit, and manage camera feeds" },
  { href: "/admin/pipeline", name: "Pipeline", desc: "Real-time data flow monitoring" },
  { href: "/admin/storage", name: "Storage", desc: "MinIO bucket sizes, purge old data, storage configuration" },
  { href: "/admin/settings", name: "Settings", desc: "Thumbnail quality, frame rate, detection parameters" },
  { href: "/admin/topology", name: "Topology", desc: "Edit camera graph, edges, and transit times" },
  { href: "/admin/retention", name: "Retention", desc: "Data retention policies by class" },
  { href: "/admin/users", name: "Users", desc: "Role definitions and permissions" },
  { href: "/admin/health", name: "Health", desc: "Embedded Grafana monitoring panels" },
  { href: "/admin/calibration", name: "Calibration", desc: "Edge filter calibration status" },
  { href: "/admin/audit", name: "Audit Log", desc: "Admin actions, data access history, and compliance reports" },
];

interface ConcurrencyStats {
  concurrent_now: number;
  concurrent_peak_5m: number;
  level: "ok" | "warning" | "critical";
  message: string | null;
  workers: number;
  warning_threshold: number;
  critical_threshold: number;
}

export default function AdminPage() {
  const [concurrency, setConcurrency] = useState<ConcurrencyStats | null>(null);

  useEffect(() => {
    fetch("/api/health/concurrency")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setConcurrency(d))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Administration</h1>

      {concurrency?.level === "warning" && (
        <div className="bg-amber-50 border border-amber-300 rounded-lg p-4 text-sm text-amber-800">
          <div className="flex items-start gap-3">
            <span className="text-amber-500 text-lg">⚠</span>
            <div>
              <p className="font-semibold">High concurrent usage detected</p>
              <p className="mt-1">
                Peak concurrent API requests in the last 5 minutes:{" "}
                <strong>{concurrency.concurrent_peak_5m}</strong> (threshold: {concurrency.warning_threshold}).
                The server is running a single worker process. If you notice slower page loads or
                search timeouts, scale to multiple workers by setting{" "}
                <code className="bg-amber-100 px-1 rounded">UVICORN_WORKERS=4</code> in the query-api
                environment.
              </p>
              <p className="mt-1 text-xs text-amber-600">
                Note: Scaling to multiple workers also requires switching the access-log cache from
                in-process to shared (Redis). See the operations guide.
              </p>
            </div>
          </div>
        </div>
      )}

      {concurrency?.level === "critical" && (
        <div className="bg-red-50 border border-red-300 rounded-lg p-4 text-sm text-red-800">
          <div className="flex items-start gap-3">
            <span className="text-red-500 text-lg">🔴</span>
            <div>
              <p className="font-semibold">Server overloaded — immediate action needed</p>
              <p className="mt-1">
                Peak concurrent API requests: <strong>{concurrency.concurrent_peak_5m}</strong>{" "}
                (critical threshold: {concurrency.critical_threshold}). Response times are likely
                degraded. Scale to multiple uvicorn workers immediately.
              </p>
              <details className="mt-2">
                <summary className="cursor-pointer text-xs font-medium text-red-700">How to fix</summary>
                <ol className="mt-2 space-y-1 text-xs list-decimal list-inside">
                  <li>
                    Set <code className="bg-red-100 px-1 rounded">UVICORN_WORKERS=4</code> in
                    docker-compose query-api environment
                  </li>
                  <li>
                    Install Redis: <code className="bg-red-100 px-1 rounded">docker compose up -d redis</code>
                  </li>
                  <li>
                    Set <code className="bg-red-100 px-1 rounded">ACCESS_LOG_CACHE=redis</code> in
                    query-api environment
                  </li>
                  <li>
                    Restart:{" "}
                    <code className="bg-red-100 px-1 rounded">
                      docker compose up -d --force-recreate query-api
                    </code>
                  </li>
                </ol>
              </details>
            </div>
          </div>
        </div>
      )}

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
        </div>
      )}

      {/* Admin sub-pages */}
      <section>
        <h2 className="text-lg font-medium mb-3">Management</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {ADMIN_SECTIONS.map((s) => (
            <Link
              key={s.href}
              href={s.href}
              className="block bg-white border border-gray-200 rounded-lg p-4 hover:shadow-md hover:border-blue-300 transition-all"
            >
              <h3 className="font-medium text-sm text-blue-700">{s.name}</h3>
              <p className="text-xs text-gray-500 mt-1">{s.desc}</p>
            </Link>
          ))}
        </div>
      </section>

      {/* Grafana dashboards */}
      <section>
        <h2 className="text-lg font-medium mb-3">Quick Access: Monitoring Dashboards</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {DASHBOARDS.map((d) => (
            <a
              key={d.path}
              href={`${GRAFANA_URL}${d.path}`}
              target="_blank"
              rel="noopener noreferrer"
              className="block bg-white border border-gray-200 rounded-lg p-4 hover:shadow-md transition-shadow"
            >
              <h3 className="font-medium text-sm">{d.name}</h3>
              <p className="text-xs text-gray-500 mt-1">{d.desc}</p>
            </a>
          ))}
        </div>
      </section>
    </div>
  );
}
