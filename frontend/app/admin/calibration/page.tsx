"use client";

import { useState, useEffect, useCallback } from "react";
import { getTopologyGraph } from "@/lib/api-client";
import type { CameraNode, TopologyGraph } from "@/lib/api-client";
import { getUserRole, isAdmin } from "@/lib/auth";

const DEFAULT_SITE = "00000000-0000-0000-0000-000000000001";

interface CalibrationInfo {
  camera_id: string;
  name: string;
  lastCalibrated: string | null;
  passThrough: number | null;
  missRate: number | null;
  falseTrigger: number | null;
}

function mockCalibrationData(cameras: CameraNode[]): CalibrationInfo[] {
  return cameras.map((cam) => ({
    camera_id: cam.camera_id,
    name: cam.name,
    // Placeholder — real data would come from artifacts/calibration/
    lastCalibrated: null,
    passThrough: null,
    missRate: null,
    falseTrigger: null,
  }));
}

export default function CalibrationPage() {
  const role = getUserRole();
  const [calibrations, setCalibrations] = useState<CalibrationInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showInstructions, setShowInstructions] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const data: TopologyGraph = await getTopologyGraph(DEFAULT_SITE);
      setCalibrations(mockCalibrationData(data.cameras));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load cameras");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Edge Filter Calibration</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {loading && <p className="text-sm text-gray-500">Loading cameras...</p>}

      {!loading && (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Camera</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Last Calibrated</th>
                <th className="text-center px-4 py-2 font-medium text-gray-600">Pass-Through</th>
                <th className="text-center px-4 py-2 font-medium text-gray-600">Miss Rate</th>
                <th className="text-center px-4 py-2 font-medium text-gray-600">False Trigger</th>
                <th className="text-right px-4 py-2 font-medium text-gray-600">Actions</th>
              </tr>
            </thead>
            <tbody>
              {calibrations.map((cal) => (
                <tr key={cal.camera_id} className="border-b border-gray-100">
                  <td className="px-4 py-2">
                    <div className="font-mono text-xs">{cal.camera_id}</div>
                    <div className="text-xs text-gray-500">{cal.name}</div>
                  </td>
                  <td className="px-4 py-2 text-xs text-gray-500">
                    {cal.lastCalibrated ?? "Never"}
                  </td>
                  <td className="px-4 py-2 text-center text-xs">
                    {cal.passThrough != null ? `${(cal.passThrough * 100).toFixed(1)}%` : "-"}
                  </td>
                  <td className="px-4 py-2 text-center text-xs">
                    {cal.missRate != null ? `${(cal.missRate * 100).toFixed(1)}%` : "-"}
                  </td>
                  <td className="px-4 py-2 text-center text-xs">
                    {cal.falseTrigger != null ? `${(cal.falseTrigger * 100).toFixed(1)}%` : "-"}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <button
                      onClick={() =>
                        setShowInstructions(
                          showInstructions === cal.camera_id ? null : cal.camera_id
                        )
                      }
                      className="text-xs text-blue-600 hover:underline"
                    >
                      Run Calibration
                    </button>
                  </td>
                </tr>
              ))}
              {calibrations.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-gray-400">
                    No cameras found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {showInstructions && (
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-4 text-sm space-y-2">
          <h3 className="font-medium">Calibration Instructions</h3>
          <p className="text-xs text-gray-600">
            Run the calibration script from the project root:
          </p>
          <pre className="bg-gray-900 text-green-400 rounded p-3 text-xs overflow-x-auto">
{`python scripts/calibration/edge_filter_calibration.py \\
  --camera-id ${showInstructions} \\
  --dsn "postgresql://user:pass@localhost:5432/cilex" \\
  --output-dir artifacts/calibration/${showInstructions}/`}
          </pre>
          <p className="text-xs text-gray-500">
            Results will be written to <code>artifacts/calibration/</code> as JSON and Markdown scorecards.
          </p>
        </div>
      )}
    </div>
  );
}
