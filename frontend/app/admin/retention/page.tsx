"use client";

import { useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface RetentionPolicy {
  dataClass: string;
  retention: string;
  storage: string;
  editable: boolean;
}

const DEFAULT_POLICIES: RetentionPolicy[] = [
  { dataClass: "Raw video", retention: "30", storage: "MinIO raw-video", editable: true },
  { dataClass: "Event clips", retention: "90", storage: "MinIO event-clips", editable: true },
  { dataClass: "Metadata (time-series)", retention: "365", storage: "TimescaleDB", editable: true },
  { dataClass: "Thumbnails", retention: "30", storage: "MinIO thumbnails", editable: true },
  { dataClass: "Embeddings", retention: "active", storage: "Kafka compaction", editable: false },
  { dataClass: "Debug traces", retention: "30", storage: "MinIO debug-traces", editable: true },
  { dataClass: "Audit logs", retention: "730", storage: "PostgreSQL", editable: true },
];

export default function RetentionPage() {
  const role = getUserRole();
  const [policies, setPolicies] = useState(DEFAULT_POLICIES);
  const [saved, setSaved] = useState(false);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  function updateRetention(index: number, value: string) {
    setPolicies((prev) => {
      const next = [...prev];
      next[index] = { ...next[index], retention: value };
      return next;
    });
    setSaved(false);
  }

  function handleSave() {
    // Local state only — no backend endpoint yet
    setSaved(true);
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Data Retention Policies</h1>

      <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 text-xs rounded p-3">
        Changes are saved locally only. Actual enforcement is via MinIO lifecycle rules
        and TimescaleDB retention policies configured in infrastructure.
      </div>

      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="text-left px-4 py-2 font-medium text-gray-600">Data Class</th>
              <th className="text-left px-4 py-2 font-medium text-gray-600">Retention (days)</th>
              <th className="text-left px-4 py-2 font-medium text-gray-600">Storage</th>
            </tr>
          </thead>
          <tbody>
            {policies.map((p, i) => (
              <tr key={p.dataClass} className="border-b border-gray-100">
                <td className="px-4 py-2 font-medium">{p.dataClass}</td>
                <td className="px-4 py-2">
                  {p.editable ? (
                    <input
                      type="number"
                      min="1"
                      value={p.retention}
                      onChange={(e) => updateRetention(i, e.target.value)}
                      className="w-24 rounded border border-gray-300 px-2 py-1 text-sm"
                    />
                  ) : (
                    <span className="text-gray-500 italic">{p.retention}</span>
                  )}
                </td>
                <td className="px-4 py-2 text-gray-500 text-xs">{p.storage}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handleSave}
          className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
        >
          Save Changes
        </button>
        {saved && (
          <span className="text-xs text-green-600">Saved (local state only)</span>
        )}
      </div>
    </div>
  );
}
