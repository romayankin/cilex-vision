"use client";

import Link from "next/link";

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

export default function AdminPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Administration</h1>

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
