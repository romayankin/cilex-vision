"use client";

const GRAFANA_URL = process.env.NEXT_PUBLIC_GRAFANA_URL || "http://localhost:3001";

const DASHBOARDS = [
  { name: "Stream Health", path: "/d/stream-health", desc: "Camera streams, frame rates, connection status" },
  { name: "Inference Performance", path: "/d/inference-perf", desc: "Detection latency, throughput, GPU utilization" },
  { name: "Bus Health", path: "/d/bus-health", desc: "Kafka topics, consumer lag, NATS metrics" },
  { name: "Storage", path: "/d/storage", desc: "MinIO usage, TimescaleDB compression, retention" },
  { name: "Model Quality", path: "/d/model-quality", desc: "Detection accuracy, Re-ID match rates, attribute confidence" },
];

export default function AdminPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Administration</h1>

      {/* Grafana dashboards */}
      <section>
        <h2 className="text-lg font-medium mb-3">Monitoring Dashboards</h2>
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

      {/* Camera management placeholder */}
      <section>
        <h2 className="text-lg font-medium mb-3">Camera Management</h2>
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-6 text-center">
          <p className="text-sm text-gray-500">
            Camera configuration and status management will be available in P3-V03.
          </p>
        </div>
      </section>

      {/* System health placeholder */}
      <section>
        <h2 className="text-lg font-medium mb-3">System Health</h2>
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-6 text-center">
          <p className="text-sm text-gray-500">
            Service health summary and alerts overview coming soon.
          </p>
        </div>
      </section>
    </div>
  );
}
