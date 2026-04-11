"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { getUserRole, isAdmin } from "@/lib/auth";
import { getSites, type SiteResponse } from "@/lib/api-client";
import SiteHealthCard, { type HealthStatus } from "@/components/SiteHealthCard";

const PAGE_SIZE = 24;

function deriveHealth(site: SiteResponse): HealthStatus {
  if (site.camera_count === 0) return "warning";
  return "healthy";
}

export default function PortalPage() {
  const router = useRouter();
  const role = getUserRole();
  const [sites, setSites] = useState<SiteResponse[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadSites = useCallback(async (newOffset = 0) => {
    try {
      setLoading(true);
      const res = await getSites({ offset: newOffset, limit: PAGE_SIZE });
      setSites(res.sites);
      setTotal(res.total);
      setOffset(newOffset);
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

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Site Portal</h1>
        <div className="flex items-center gap-2">
          <Link
            href="/portal/comparison"
            className="px-3 py-1.5 border border-gray-300 text-sm rounded hover:bg-gray-50"
          >
            Compare Sites
          </Link>
          <Link
            href="/portal/sites/new"
            className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
          >
            New Site
          </Link>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {loading && <p className="text-sm text-gray-500">Loading sites...</p>}

      {!loading && sites.length > 0 && (
        <>
          <div className="text-sm text-gray-500">
            Showing {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} of {total} sites
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            {sites.map((site) => (
              <SiteHealthCard
                key={site.site_id}
                siteId={site.site_id}
                name={site.name}
                cameraCount={site.camera_count}
                activeAlerts={0}
                storagePct={0}
                status={deriveHealth(site)}
                onClick={() => router.push(`/portal/sites/${site.site_id}/settings`)}
              />
            ))}
          </div>

          {total > PAGE_SIZE && (
            <div className="flex items-center justify-center gap-4 pt-4">
              <button
                onClick={() => loadSites(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0}
                className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Previous
              </button>
              <button
                onClick={() => loadSites(offset + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= total}
                className="px-4 py-2 text-sm border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Next
              </button>
            </div>
          )}
        </>
      )}

      {!loading && sites.length === 0 && !error && (
        <div className="text-center py-12 text-gray-400">
          No sites configured. Click &ldquo;New Site&rdquo; to create one.
        </div>
      )}
    </div>
  );
}
