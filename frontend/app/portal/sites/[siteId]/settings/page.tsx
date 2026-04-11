"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { getUserRole, isAdmin } from "@/lib/auth";
import { getSite, updateSite, getTopologyGraph, type SiteResponse, type TopologyGraph } from "@/lib/api-client";

const TIMEZONES = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "Asia/Tokyo",
  "Asia/Shanghai",
  "Asia/Kolkata",
  "Australia/Sydney",
];

export default function SiteSettingsPage() {
  const params = useParams();
  const router = useRouter();
  const role = getUserRole();
  const siteId = params.siteId as string;

  const [site, setSite] = useState<SiteResponse | null>(null);
  const [topology, setTopology] = useState<TopologyGraph | null>(null);
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [timezone, setTimezone] = useState("UTC");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const [siteData, topoData] = await Promise.all([
        getSite(siteId),
        getTopologyGraph(siteId).catch(() => null),
      ]);
      setSite(siteData);
      setName(siteData.name);
      setAddress(siteData.address ?? "");
      setTimezone(siteData.timezone);
      setTopology(topoData);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load site");
    } finally {
      setLoading(false);
    }
  }, [siteId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setError("Site name is required.");
      return;
    }

    try {
      setSaving(true);
      setError(null);
      setSaved(false);
      await updateSite(siteId, {
        name: name.trim(),
        address: address.trim() || null,
        timezone,
      });
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save site");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <p className="text-sm text-gray-500">Loading site settings...</p>;
  }

  if (!site) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-red-600">Site not found: {siteId}</p>
        <button onClick={() => router.push("/portal")} className="text-sm text-blue-600 hover:underline">
          Back to Portal
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-2xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Site Settings</h1>
        <button
          onClick={() => router.push("/portal")}
          className="text-sm text-blue-600 hover:underline"
        >
          Back to Portal
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {saved && (
        <div className="bg-green-50 border border-green-200 text-green-700 text-sm rounded p-3">
          Site settings saved.
        </div>
      )}

      {/* Settings form */}
      <form onSubmit={handleSave} className="bg-white border border-gray-200 rounded-lg p-4 space-y-4">
        <div>
          <label htmlFor="edit-name" className="block text-sm font-medium text-gray-700 mb-1">
            Site Name *
          </label>
          <input
            id="edit-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
        </div>

        <div>
          <label htmlFor="edit-address" className="block text-sm font-medium text-gray-700 mb-1">
            Address
          </label>
          <input
            id="edit-address"
            type="text"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
        </div>

        <div>
          <label htmlFor="edit-tz" className="block text-sm font-medium text-gray-700 mb-1">
            Timezone
          </label>
          <select
            id="edit-tz"
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          >
            {TIMEZONES.map((tz) => (
              <option key={tz} value={tz}>
                {tz}
              </option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2 pt-2">
          <button
            type="submit"
            disabled={saving}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      </form>

      {/* Camera list */}
      <section>
        <h2 className="text-lg font-medium mb-3">Cameras ({topology?.cameras.length ?? 0})</h2>
        {topology && topology.cameras.length > 0 ? (
          <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>
                  <th className="text-left px-4 py-2 font-medium text-gray-600">Camera ID</th>
                  <th className="text-left px-4 py-2 font-medium text-gray-600">Name</th>
                  <th className="text-left px-4 py-2 font-medium text-gray-600">Zone</th>
                  <th className="text-left px-4 py-2 font-medium text-gray-600">Status</th>
                </tr>
              </thead>
              <tbody>
                {topology.cameras.map((cam) => (
                  <tr key={cam.camera_id} className="border-b border-gray-100 hover:bg-gray-50">
                    <td className="px-4 py-2 font-mono text-xs">{cam.camera_id}</td>
                    <td className="px-4 py-2">{cam.name}</td>
                    <td className="px-4 py-2 text-gray-500">{cam.zone_id ?? "-"}</td>
                    <td className="px-4 py-2">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                          cam.status === "online"
                            ? "bg-green-100 text-green-700"
                            : "bg-red-100 text-red-700"
                        }`}
                      >
                        {cam.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-gray-400">No cameras assigned to this site.</p>
        )}
      </section>

      {/* Retention policy display */}
      <section>
        <h2 className="text-lg font-medium mb-3">Retention Policy</h2>
        <div className="bg-white border border-gray-200 rounded-lg p-4">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <span className="text-gray-500">Frame blobs:</span>
              <span className="ml-2 font-medium">7 days (hot)</span>
            </div>
            <div>
              <span className="text-gray-500">Event clips:</span>
              <span className="ml-2 font-medium">90 days (warm)</span>
            </div>
            <div>
              <span className="text-gray-500">Detections:</span>
              <span className="ml-2 font-medium">30 days</span>
            </div>
            <div>
              <span className="text-gray-500">Debug traces:</span>
              <span className="ml-2 font-medium">30 days (cold)</span>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
