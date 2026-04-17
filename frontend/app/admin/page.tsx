"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  Video,
  Map,
  Compass,
  Ruler,
  Cpu,
  Activity,
  Target,
  Sliders,
  Database,
  Archive,
  Server,
  HeartPulse,
  MemoryStick,
  Users,
  ShieldCheck,
  BarChart3,
  ExternalLink,
  type LucideIcon,
} from "lucide-react";

const GRAFANA_URL = process.env.NEXT_PUBLIC_GRAFANA_URL || "http://localhost:3001";

interface AdminPage {
  href: string;
  name: string;
  desc: string;
  Icon: LucideIcon;
}

interface AdminGroup {
  title: string;
  subtitle: string;
  Icon: LucideIcon;
  accent: string;
  pages: AdminPage[];
}

const ADMIN_GROUPS: AdminGroup[] = [
  {
    title: "Cameras & Scenes",
    subtitle: "Physical sensors and spatial configuration",
    Icon: Video,
    accent: "blue",
    pages: [
      { href: "/admin/cameras",     name: "Cameras",     desc: "Add, edit, and manage camera feeds", Icon: Video },
      { href: "/admin/zones",       name: "Zones",       desc: "Draw detection and loitering zones on camera snapshots", Icon: Map },
      { href: "/admin/topology",    name: "Topology",    desc: "Edit camera graph, edges, and transit times", Icon: Compass },
      { href: "/admin/calibration", name: "Calibration", desc: "Edge filter calibration status", Icon: Ruler },
    ],
  },
  {
    title: "AI & Pipeline",
    subtitle: "Processing intelligence and data flow",
    Icon: Cpu,
    accent: "indigo",
    pages: [
      { href: "/admin/inference", name: "Inference",        desc: "Pipeline monitor + per-model detection latency and throughput", Icon: Cpu },
      { href: "/admin/pipeline",  name: "Pipeline",         desc: "Real-time data flow monitoring", Icon: Activity },
      { href: "/admin/planner",   name: "Use Case Planner", desc: "Select AI models and see which surveillance use cases become feasible", Icon: Target },
      { href: "/admin/settings",  name: "Settings",         desc: "Thumbnail quality, frame rate, detection parameters", Icon: Sliders },
    ],
  },
  {
    title: "Data",
    subtitle: "Data lifecycle and retention",
    Icon: Database,
    accent: "emerald",
    pages: [
      { href: "/admin/storage",   name: "Storage",   desc: "MinIO bucket sizes, purge old data, storage configuration", Icon: Database },
      { href: "/admin/retention", name: "Retention", desc: "Data retention policies by class", Icon: Archive },
    ],
  },
  {
    title: "System",
    subtitle: "Infrastructure and monitoring",
    Icon: Server,
    accent: "amber",
    pages: [
      { href: "/admin/services",  name: "Services",  desc: "Monitor and restart Docker containers", Icon: Server },
      { href: "/admin/resources", name: "Resources", desc: "Memory and CPU limits per service", Icon: MemoryStick },
      { href: "/admin/health",    name: "Health",    desc: "Embedded Grafana monitoring panels", Icon: HeartPulse },
    ],
  },
  {
    title: "Access & Audit",
    subtitle: "Security and governance",
    Icon: ShieldCheck,
    accent: "rose",
    pages: [
      { href: "/admin/users", name: "Users",      desc: "Role definitions and permissions", Icon: Users },
      { href: "/admin/audit", name: "Audit Log",  desc: "Admin actions, data access history, and compliance reports", Icon: ShieldCheck },
    ],
  },
];

const DASHBOARDS = [
  { name: "Stream Health",          path: "/d/stream-health",  desc: "Camera streams, frame rates, connection status" },
  { name: "Inference Performance",  path: "/d/inference-perf", desc: "Detection latency, throughput, GPU utilization" },
  { name: "Bus Health",             path: "/d/bus-health",     desc: "Kafka topics, consumer lag, NATS metrics" },
  { name: "Storage",                path: "/d/storage",        desc: "MinIO usage, TimescaleDB compression, retention" },
  { name: "Model Quality",          path: "/d/model-quality",  desc: "Detection accuracy, Re-ID match rates, attribute confidence" },
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

const ACCENT_CLASSES: Record<
  string,
  { bg: string; text: string; border: string; hoverText: string }
> = {
  blue:    { bg: "bg-blue-50",    text: "text-blue-600",    border: "hover:border-blue-300",    hoverText: "group-hover:text-blue-600" },
  indigo:  { bg: "bg-indigo-50",  text: "text-indigo-600",  border: "hover:border-indigo-300",  hoverText: "group-hover:text-indigo-600" },
  emerald: { bg: "bg-emerald-50", text: "text-emerald-600", border: "hover:border-emerald-300", hoverText: "group-hover:text-emerald-600" },
  amber:   { bg: "bg-amber-50",   text: "text-amber-600",   border: "hover:border-amber-300",   hoverText: "group-hover:text-amber-600" },
  rose:    { bg: "bg-rose-50",    text: "text-rose-600",    border: "hover:border-rose-300",    hoverText: "group-hover:text-rose-600" },
};

export default function AdminPage() {
  const [concurrency, setConcurrency] = useState<ConcurrencyStats | null>(null);

  useEffect(() => {
    fetch("/api/health/concurrency")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setConcurrency(d))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-8 pb-12">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold text-gray-900">Administration</h1>
        <span className="text-xs text-gray-500">
          {ADMIN_GROUPS.reduce((n, g) => n + g.pages.length, 0)} pages across {ADMIN_GROUPS.length} sections
        </span>
      </div>

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
                  <li>Set <code className="bg-red-100 px-1 rounded">UVICORN_WORKERS=4</code> in docker-compose query-api environment</li>
                  <li>Install Redis: <code className="bg-red-100 px-1 rounded">docker compose up -d redis</code></li>
                  <li>Set <code className="bg-red-100 px-1 rounded">ACCESS_LOG_CACHE=redis</code> in query-api environment</li>
                  <li>Restart: <code className="bg-red-100 px-1 rounded">docker compose up -d --force-recreate query-api</code></li>
                </ol>
              </details>
            </div>
          </div>
        </div>
      )}

      {concurrency && (
        <div className="bg-white border border-gray-200 rounded-lg p-3 text-sm">
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

      {ADMIN_GROUPS.map((group) => {
        const accent = ACCENT_CLASSES[group.accent] ?? ACCENT_CLASSES.blue;
        return (
          <section key={group.title} className="space-y-3">
            <div className="flex items-center gap-3">
              <div className={`${accent.bg} ${accent.text} rounded-lg p-2 flex items-center justify-center`}>
                <group.Icon className="w-5 h-5" strokeWidth={2} />
              </div>
              <div>
                <h2 className="text-base font-semibold text-gray-900 leading-tight">{group.title}</h2>
                <p className="text-xs text-gray-500 leading-tight">{group.subtitle}</p>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              {group.pages.map((page) => (
                <Link
                  key={page.href}
                  href={page.href}
                  className={`group block bg-white border border-gray-200 rounded-lg p-3 transition-all ${accent.border} hover:shadow-sm`}
                >
                  <div className="flex items-start gap-2.5">
                    <div className={`${accent.text} mt-0.5 flex-shrink-0`}>
                      <page.Icon className="w-4 h-4" strokeWidth={2} />
                    </div>
                    <div className="min-w-0">
                      <h3 className={`font-medium text-sm text-gray-900 ${accent.hoverText} transition-colors`}>
                        {page.name}
                      </h3>
                      <p className="text-xs text-gray-500 mt-0.5 leading-snug">{page.desc}</p>
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          </section>
        );
      })}

      <section className="space-y-3 pt-4 border-t border-gray-200">
        <div className="flex items-center gap-3">
          <div className="bg-gray-100 text-gray-600 rounded-lg p-2 flex items-center justify-center">
            <BarChart3 className="w-5 h-5" strokeWidth={2} />
          </div>
          <div>
            <h2 className="text-base font-semibold text-gray-900 leading-tight">Monitoring Dashboards</h2>
            <p className="text-xs text-gray-500 leading-tight">External Grafana dashboards (open in new tab)</p>
          </div>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {DASHBOARDS.map((d) => (
            <a
              key={d.path}
              href={`${GRAFANA_URL}${d.path}`}
              target="_blank"
              rel="noopener noreferrer"
              className="group block bg-white border border-gray-200 rounded-lg p-3 hover:shadow-sm hover:border-gray-300 transition-all"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="font-medium text-sm text-gray-900 group-hover:text-gray-700">{d.name}</h3>
                  <p className="text-xs text-gray-500 mt-0.5 leading-snug">{d.desc}</p>
                </div>
                <ExternalLink className="w-3.5 h-3.5 text-gray-400 flex-shrink-0 mt-0.5" />
              </div>
            </a>
          ))}
        </div>
      </section>
    </div>
  );
}
