"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, Cpu, MemoryStick, Terminal } from "lucide-react";
import { isAdmin, getUserRole } from "@/lib/auth";

interface ServiceLimit {
  name: string;
  state: string;
  mem_limit_mb: number | null;
  memswap_limit_mb: number | null;
  swap_allowed_mb: number | null;
  cpus: number | null;
  has_mem_limit: boolean;
  has_cpu_limit: boolean;
}

interface LimitsResponse {
  host: { total_mem_gb: number; total_cpus: number };
  totals: {
    mem_allocated_gb: number;
    mem_allocated_pct: number;
    cpu_allocated: number;
    cpu_allocated_pct: number;
    services_with_limits: number;
    services_without_limits: number;
    total_services: number;
  };
  services: ServiceLimit[];
}

export default function ResourcesPage() {
  const role = getUserRole();
  const [data, setData] = useState<LimitsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isAdmin(role)) return;
    fetch("/api/resources/limits", { credentials: "include" })
      .then(async (r) => {
        if (!r.ok) {
          setError(`HTTP ${r.status}`);
          return;
        }
        setData(await r.json());
      })
      .catch((e) => setError(e.message));
  }, [role]);

  if (!isAdmin(role)) {
    return <div className="text-sm text-gray-500">Admin access required.</div>;
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 text-red-700 rounded-lg p-3 text-sm">
        Failed to load resource limits: {error}
      </div>
    );
  }

  if (!data) {
    return <div className="text-sm text-gray-500">Loading…</div>;
  }

  const memPctColor =
    data.totals.mem_allocated_pct > 80
      ? "text-red-600"
      : data.totals.mem_allocated_pct > 70
        ? "text-amber-600"
        : "text-emerald-600";

  const cpuPctColor =
    data.totals.cpu_allocated_pct > 85
      ? "text-red-600"
      : data.totals.cpu_allocated_pct > 75
        ? "text-amber-600"
        : "text-emerald-600";

  return (
    <div className="space-y-6 pb-12">
      <div>
        <h1 className="text-xl font-semibold">Resource Limits</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Read-only view of Docker memory and CPU limits across all services.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-white border border-gray-200 rounded-lg p-4">
          <div className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wide mb-1">
            <MemoryStick className="w-3.5 h-3.5" />
            Memory Allocated
          </div>
          <div className={`text-2xl font-semibold font-mono ${memPctColor}`}>
            {data.totals.mem_allocated_gb} GB
          </div>
          <div className="text-xs text-gray-500 mt-0.5">
            {data.totals.mem_allocated_pct}% of {data.host.total_mem_gb} GB host
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-lg p-4">
          <div className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wide mb-1">
            <Cpu className="w-3.5 h-3.5" />
            CPU Allocated
          </div>
          <div className={`text-2xl font-semibold font-mono ${cpuPctColor}`}>
            {data.totals.cpu_allocated} / {data.host.total_cpus}
          </div>
          <div className="text-xs text-gray-500 mt-0.5">
            {data.totals.cpu_allocated_pct}% of {data.host.total_cpus} logical cores
          </div>
        </div>

        <div className="bg-white border border-gray-200 rounded-lg p-4">
          <div className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wide mb-1">
            <AlertTriangle className="w-3.5 h-3.5" />
            Coverage
          </div>
          <div className="text-2xl font-semibold font-mono text-gray-900">
            {data.totals.services_with_limits}/{data.totals.total_services}
          </div>
          <div className="text-xs text-gray-500 mt-0.5">
            services have mem limits ·{" "}
            {data.totals.services_without_limits > 0 ? (
              <span className="text-amber-600">
                {data.totals.services_without_limits} unlimited
              </span>
            ) : (
              <span className="text-emerald-600">all covered</span>
            )}
          </div>
        </div>
      </div>

      <section className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-gray-700 space-y-3">
        <h2 className="font-semibold text-gray-900 flex items-center gap-2">
          <Terminal className="w-4 h-4" />
          How to change these limits
        </h2>
        <ol className="list-decimal list-inside space-y-2">
          <li>
            Edit <code className="bg-white px-1.5 py-0.5 rounded border border-blue-100 font-mono text-xs">infra/docker-compose.yml</code> on the server.
          </li>
          <li>
            Find the service block and modify the <code className="bg-white px-1.5 py-0.5 rounded border border-blue-100 font-mono text-xs">mem_limit</code>,{" "}
            <code className="bg-white px-1.5 py-0.5 rounded border border-blue-100 font-mono text-xs">memswap_limit</code>, and{" "}
            <code className="bg-white px-1.5 py-0.5 rounded border border-blue-100 font-mono text-xs">cpus</code> values.
          </li>
          <li>
            <strong>Keep <code className="bg-white px-1.5 py-0.5 rounded border border-blue-100 font-mono text-xs">memswap_limit</code> equal to <code className="bg-white px-1.5 py-0.5 rounded border border-blue-100 font-mono text-xs">mem_limit</code></strong> — this disables swap for that container. Swap thrashing is what causes the host OS to freeze.
          </li>
          <li>
            Apply changes with:{" "}
            <code className="block mt-1 bg-gray-900 text-green-300 p-2 rounded font-mono text-xs whitespace-pre">
              docker compose -f infra/docker-compose.yml up -d --force-recreate &lt;service&gt;
            </code>
          </li>
          <li>
            To apply to all services at once:{" "}
            <code className="block mt-1 bg-gray-900 text-green-300 p-2 rounded font-mono text-xs whitespace-pre">
              docker compose -f infra/docker-compose.yml up -d --force-recreate
            </code>
          </li>
          <li>Refresh this page to see the new values.</li>
        </ol>

        <div className="pt-2 border-t border-blue-200 text-xs">
          <strong>Guidelines for a 16 GB host:</strong>
          <ul className="list-disc list-inside mt-1 space-y-0.5">
            <li>Keep total memory allocation ≤ 70% (11.2 GB) so the OS has room for desktop + page cache</li>
            <li>Keep total CPU allocation ≤ 80% (16 cores) so the UI stays responsive</li>
            <li>Never set <code className="font-mono">memswap_limit</code> higher than <code className="font-mono">mem_limit</code> — that enables swap and causes freezes</li>
            <li>When in doubt, restart inference-worker and ollama first — those are the biggest resource consumers</li>
          </ul>
        </div>
      </section>

      <section className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500 tracking-wide">
            <tr>
              <th className="text-left px-4 py-2 font-medium">Service</th>
              <th className="text-left px-4 py-2 font-medium">State</th>
              <th className="text-right px-4 py-2 font-medium">Memory</th>
              <th className="text-right px-4 py-2 font-medium">Swap allowed</th>
              <th className="text-right px-4 py-2 font-medium">CPU</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {data.services.map((s) => (
              <tr key={s.name} className="hover:bg-gray-50">
                <td className="px-4 py-2 font-mono text-xs text-gray-900">{s.name}</td>
                <td className="px-4 py-2 text-xs">
                  <span
                    className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium ${
                      s.state === "running"
                        ? "bg-emerald-50 text-emerald-700"
                        : "bg-gray-100 text-gray-600"
                    }`}
                  >
                    {s.state}
                  </span>
                </td>
                <td className="px-4 py-2 text-right font-mono text-xs">
                  {s.mem_limit_mb !== null ? (
                    <span className="text-gray-900">{s.mem_limit_mb} MB</span>
                  ) : (
                    <span className="text-amber-600 font-medium">unlimited</span>
                  )}
                </td>
                <td className="px-4 py-2 text-right font-mono text-xs">
                  {s.swap_allowed_mb !== null ? (
                    s.swap_allowed_mb === 0 ? (
                      <span className="text-emerald-600">disabled</span>
                    ) : (
                      <span className="text-amber-600">{s.swap_allowed_mb} MB</span>
                    )
                  ) : (
                    <span className="text-gray-400">—</span>
                  )}
                </td>
                <td className="px-4 py-2 text-right font-mono text-xs">
                  {s.cpus !== null ? (
                    <span className="text-gray-900">{s.cpus} cores</span>
                  ) : (
                    <span className="text-amber-600 font-medium">unlimited</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="text-xs text-gray-500 italic">
        Values read from Docker Engine API (<code className="font-mono">docker inspect</code>).
        If a service shows &ldquo;unlimited&rdquo;, the limit is missing from docker-compose.yml and the
        service can consume all host resources — fix this.
      </div>
    </div>
  );
}
