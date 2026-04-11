"use client";

import { useCallback, useEffect, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";
import { getSites, type SiteResponse } from "@/lib/api-client";

const MAX_COMPARE = 5;

interface SiteMetrics {
  siteId: string;
  name: string;
  cameraCount: number;
  detectionRate: number;
  alertFrequency: number;
  storageGb: number;
}

function deriveMockMetrics(site: SiteResponse): SiteMetrics {
  return {
    siteId: site.site_id,
    name: site.name,
    cameraCount: site.camera_count,
    detectionRate: site.camera_count * 120,
    alertFrequency: Math.max(0, site.camera_count * 2 - 3),
    storageGb: site.camera_count * 15,
  };
}

function BarChart({ values, labels, maxVal }: { values: number[]; labels: string[]; maxVal: number }) {
  const barMax = maxVal || 1;
  return (
    <div className="flex items-end gap-2 h-32">
      {values.map((v, i) => (
        <div key={labels[i]} className="flex flex-col items-center flex-1 min-w-0">
          <div className="text-xs text-gray-600 font-medium mb-1">{v}</div>
          <div
            className="w-full bg-blue-500 rounded-t"
            style={{ height: `${Math.max((v / barMax) * 100, 2)}%` }}
          />
          <div className="text-xs text-gray-500 mt-1 truncate w-full text-center" title={labels[i]}>
            {labels[i]}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function ComparisonPage() {
  const role = getUserRole();
  const [allSites, setAllSites] = useState<SiteResponse[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadSites = useCallback(async () => {
    try {
      setLoading(true);
      const res = await getSites({ limit: 200 });
      setAllSites(res.sites);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load sites");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSites();
  }, [loadSites]);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  function toggleSite(siteId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(siteId)) {
        next.delete(siteId);
      } else if (next.size < MAX_COMPARE) {
        next.add(siteId);
      }
      return next;
    });
  }

  const compared = allSites
    .filter((s) => selected.has(s.site_id))
    .map(deriveMockMetrics);

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Cross-Site Comparison</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {loading && <p className="text-sm text-gray-500">Loading sites...</p>}

      {!loading && (
        <>
          {/* Site selector */}
          <div className="bg-white border border-gray-200 rounded-lg p-4">
            <h2 className="text-sm font-medium text-gray-700 mb-2">
              Select sites to compare (max {MAX_COMPARE}):
            </h2>
            <div className="flex flex-wrap gap-2">
              {allSites.map((site) => (
                <button
                  key={site.site_id}
                  onClick={() => toggleSite(site.site_id)}
                  disabled={!selected.has(site.site_id) && selected.size >= MAX_COMPARE}
                  className={`px-3 py-1.5 text-xs rounded border transition-colors ${
                    selected.has(site.site_id)
                      ? "bg-blue-100 border-blue-300 text-blue-700"
                      : "bg-white border-gray-300 text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                  }`}
                >
                  {site.name}
                </button>
              ))}
            </div>
          </div>

          {compared.length >= 2 && (
            <>
              {/* Comparison table */}
              <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 border-b border-gray-200">
                    <tr>
                      <th className="text-left px-4 py-2 font-medium text-gray-600">Metric</th>
                      {compared.map((s) => (
                        <th key={s.siteId} className="text-right px-4 py-2 font-medium text-gray-600">
                          {s.name}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    <tr className="border-b border-gray-100">
                      <td className="px-4 py-2 text-gray-700">Camera Count</td>
                      {compared.map((s) => (
                        <td key={s.siteId} className="text-right px-4 py-2 font-mono">
                          {s.cameraCount}
                        </td>
                      ))}
                    </tr>
                    <tr className="border-b border-gray-100">
                      <td className="px-4 py-2 text-gray-700">Detection Rate (est/hr)</td>
                      {compared.map((s) => (
                        <td key={s.siteId} className="text-right px-4 py-2 font-mono">
                          {s.detectionRate}
                        </td>
                      ))}
                    </tr>
                    <tr className="border-b border-gray-100">
                      <td className="px-4 py-2 text-gray-700">Alert Frequency (/day)</td>
                      {compared.map((s) => (
                        <td key={s.siteId} className="text-right px-4 py-2 font-mono">
                          {s.alertFrequency}
                        </td>
                      ))}
                    </tr>
                    <tr>
                      <td className="px-4 py-2 text-gray-700">Storage (GB)</td>
                      {compared.map((s) => (
                        <td key={s.siteId} className="text-right px-4 py-2 font-mono">
                          {s.storageGb}
                        </td>
                      ))}
                    </tr>
                  </tbody>
                </table>
              </div>

              {/* Bar charts */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="bg-white border border-gray-200 rounded-lg p-4">
                  <h3 className="text-sm font-medium text-gray-700 mb-3">Camera Count</h3>
                  <BarChart
                    values={compared.map((s) => s.cameraCount)}
                    labels={compared.map((s) => s.name)}
                    maxVal={Math.max(...compared.map((s) => s.cameraCount))}
                  />
                </div>
                <div className="bg-white border border-gray-200 rounded-lg p-4">
                  <h3 className="text-sm font-medium text-gray-700 mb-3">Detection Rate (est/hr)</h3>
                  <BarChart
                    values={compared.map((s) => s.detectionRate)}
                    labels={compared.map((s) => s.name)}
                    maxVal={Math.max(...compared.map((s) => s.detectionRate))}
                  />
                </div>
                <div className="bg-white border border-gray-200 rounded-lg p-4">
                  <h3 className="text-sm font-medium text-gray-700 mb-3">Alert Frequency (/day)</h3>
                  <BarChart
                    values={compared.map((s) => s.alertFrequency)}
                    labels={compared.map((s) => s.name)}
                    maxVal={Math.max(...compared.map((s) => s.alertFrequency))}
                  />
                </div>
                <div className="bg-white border border-gray-200 rounded-lg p-4">
                  <h3 className="text-sm font-medium text-gray-700 mb-3">Storage (GB)</h3>
                  <BarChart
                    values={compared.map((s) => s.storageGb)}
                    labels={compared.map((s) => s.name)}
                    maxVal={Math.max(...compared.map((s) => s.storageGb))}
                  />
                </div>
              </div>
            </>
          )}

          {selected.size > 0 && selected.size < 2 && (
            <p className="text-sm text-gray-500">Select at least 2 sites to compare.</p>
          )}

          {selected.size === 0 && (
            <p className="text-sm text-gray-400">Select sites above to begin comparison.</p>
          )}
        </>
      )}
    </div>
  );
}
