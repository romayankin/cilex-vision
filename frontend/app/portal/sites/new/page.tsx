"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { getUserRole, isAdmin } from "@/lib/auth";
import { createSite } from "@/lib/api-client";

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

export default function NewSitePage() {
  const router = useRouter();
  const role = getUserRole();
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [timezone, setTimezone] = useState("UTC");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setError("Site name is required.");
      return;
    }

    try {
      setSubmitting(true);
      setError(null);
      await createSite({ name: name.trim(), address: address.trim() || null, timezone });
      router.push("/portal");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create site");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="max-w-lg space-y-4">
      <h1 className="text-xl font-semibold">Create New Site</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="bg-white border border-gray-200 rounded-lg p-4 space-y-4">
        <div>
          <label htmlFor="site-name" className="block text-sm font-medium text-gray-700 mb-1">
            Site Name *
          </label>
          <input
            id="site-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Downtown Campus"
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
        </div>

        <div>
          <label htmlFor="site-address" className="block text-sm font-medium text-gray-700 mb-1">
            Address
          </label>
          <input
            id="site-address"
            type="text"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="e.g. 123 Main St, City"
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
        </div>

        <div>
          <label htmlFor="site-tz" className="block text-sm font-medium text-gray-700 mb-1">
            Timezone
          </label>
          <select
            id="site-tz"
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
            disabled={submitting}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? "Creating..." : "Create Site"}
          </button>
          <button
            type="button"
            onClick={() => router.push("/portal")}
            className="px-4 py-2 border border-gray-300 text-sm rounded hover:bg-gray-50"
          >
            Cancel
          </button>
        </div>
      </form>
    </div>
  );
}
