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
      <div>
        <h1 className="text-xl font-semibold">
          Edge Filter Calibration
          <span className="ml-2 text-sm font-normal text-gray-400">(Предфильтр)</span>
        </h1>
        <p className="text-sm text-gray-500">
          Configure per-camera frame filtering to reduce inference load and scale to more cameras.
        </p>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {loading && <p className="text-sm text-gray-500">Loading cameras...</p>}

      {!loading && calibrations.length < 6 && (
        <div className="bg-green-50 border border-green-200 text-green-800 rounded-lg p-4 text-sm">
          <div className="flex items-start gap-3">
            <span className="text-green-500 text-lg">✓</span>
            <div>
              <p className="font-semibold">Edge filtering is not needed yet</p>
              <p className="mt-1">
                You have {calibrations.length} camera{calibrations.length !== 1 ? "s" : ""}.
                The inference pipeline can process up to 7 cameras at 2fps on CPU without
                filtering. Edge filtering becomes important at 6+ cameras to reduce unnecessary
                processing and extend capacity.
              </p>
            </div>
          </div>
        </div>
      )}

      {!loading && calibrations.length >= 6 && calibrations.length < 15 && (
        <div className="bg-amber-50 border border-amber-300 rounded-lg p-4 text-sm text-amber-800">
          <div className="flex items-start gap-3">
            <span className="text-amber-500 text-lg">⚠</span>
            <div>
              <p className="font-semibold">Consider enabling edge filtering</p>
              <p className="mt-1">
                You have {calibrations.length} cameras. At 2fps per camera, the inference
                pipeline is processing {calibrations.length * 2} frames/sec. Your CPU
                (i5-13500) can handle ~14fps with YOLOv8s. Edge filtering on low-activity
                cameras (e.g., empty corridors, parking lots at night) could reduce load by
                40–80%, freeing capacity for additional cameras.
              </p>
            </div>
          </div>
        </div>
      )}

      {!loading && calibrations.length >= 15 && (
        <div className="bg-red-50 border border-red-300 rounded-lg p-4 text-sm text-red-800">
          <div className="flex items-start gap-3">
            <span className="text-red-500 text-lg">🔴</span>
            <div>
              <p className="font-semibold">Edge filtering is strongly recommended</p>
              <p className="mt-1">
                You have {calibrations.length} cameras generating {calibrations.length * 2} frames/sec.
                Without edge filtering, the inference pipeline will drop frames or fall behind.
                Enable filtering on all cameras except high-priority entrances to reduce
                processing load to a sustainable level.
              </p>
              {calibrations.length > 20 && (
                <p className="mt-1">
                  At this scale, also consider adding an RTX A2000 GPU — see the{" "}
                  <a href="/admin/planner" className="underline">Use Case Planner</a> for
                  hardware capacity estimates.
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      <section className="bg-white border border-gray-200 rounded-lg p-5 space-y-4">
        <h2 className="font-medium text-base">
          About Edge Filtering
          <span className="ml-2 text-xs font-normal text-gray-400">(Предварительная фильтрация кадров)</span>
        </h2>

        <div className="text-sm text-gray-600 space-y-3 leading-relaxed">
          <p>
            <strong>What it is:</strong> An edge filter (предфильтр) sits between the camera
            and the AI inference pipeline. Before sending a frame for object detection, it
            decides: &quot;Is anything happening in this frame that&apos;s worth processing?&quot; If nothing
            changed since the last frame — no motion, no new objects — the frame is dropped
            before it reaches the AI model, saving CPU time and bandwidth.
          </p>

          <div className="bg-gray-50 border border-gray-200 rounded p-3 font-mono text-xs text-gray-700">
            <div>Camera → Edge Agent → <strong>[Edge Filter: worth processing?]</strong></div>
            <div className="ml-32">↓ YES → NATS → Decode → Inference (YOLO)</div>
            <div className="ml-32">↓ NO  → Frame dropped (saves CPU)</div>
          </div>

          <p>
            <strong>Why it matters:</strong> Without filtering, every camera sends 2 frames per
            second regardless of activity. A quiet hallway at 3 AM generates the same load as a
            busy entrance at noon. With edge filtering, a quiet camera might only send 1 frame
            every 5–10 seconds (90% reduction), while a busy camera still sends 2fps. This
            dramatically increases how many cameras the system can handle.
          </p>

          <div className="bg-gray-50 border border-gray-200 rounded p-3">
            <p className="font-medium text-gray-700 text-xs uppercase tracking-wide mb-2">
              Scaling impact
            </p>
            <table className="w-full text-xs">
              <tbody>
                <tr className="border-b border-gray-200">
                  <td className="py-1 text-gray-500">Without filtering (current)</td>
                  <td className="py-1 text-right font-mono">2 fps × every camera = full load</td>
                </tr>
                <tr className="border-b border-gray-200">
                  <td className="py-1 text-gray-500">With filtering (quiet cameras)</td>
                  <td className="py-1 text-right font-mono">0.1–0.5 fps = 75–95% reduction</td>
                </tr>
                <tr className="border-b border-gray-200">
                  <td className="py-1 text-gray-500">Max cameras on CPU without filtering</td>
                  <td className="py-1 text-right font-mono">~7 (YOLOv8s at 14fps ÷ 2fps)</td>
                </tr>
                <tr>
                  <td className="py-1 text-gray-500">Max cameras on CPU with filtering</td>
                  <td className="py-1 text-right font-mono">~20–30 (depending on activity)</td>
                </tr>
              </tbody>
            </table>
          </div>

          <p>
            <strong>How calibration works:</strong> Each camera needs to be calibrated
            individually because every scene is different. A camera watching a doorway
            needs sensitive motion detection; a camera watching an open field can be
            aggressive about filtering. Calibration runs a test period (typically 1 hour)
            where it processes ALL frames and then analyzes which ones actually had
            meaningful detections — the result tells you the optimal filter threshold
            for that camera.
          </p>
        </div>
      </section>

      <section className="bg-white border border-gray-200 rounded-lg p-5 space-y-3">
        <h2 className="font-medium text-base">Calibration Metrics</h2>
        <div className="text-sm text-gray-600 space-y-2">
          <p>
            <strong>Pass-Through Rate:</strong> The percentage of frames the edge filter
            sends to inference (vs dropping as &quot;no activity&quot;). Lower is more aggressive
            filtering. A busy entrance might have 60–80% pass-through; a quiet corridor
            should be 5–20%.
          </p>
          <p>
            <strong>Miss Rate:</strong> The percentage of frames the filter dropped
            that actually contained a real detection. This is the danger metric — if
            it&apos;s above 2–3%, the filter is too aggressive and missing real events.
            Target: below 1%.
          </p>
          <p>
            <strong>False Trigger Rate:</strong> The percentage of frames the filter
            sent to inference that turned out to have nothing in them (wasted
            processing). Higher means the filter isn&apos;t aggressive enough.
            Target: below 30%.
          </p>
        </div>
      </section>

      {!loading && (
        <section>
        <div className="flex items-center gap-2 mb-2">
          <h2 className="font-medium text-base">Per-Camera Calibration Status</h2>
          <span className="text-xs bg-gray-100 text-gray-500 rounded px-2 py-0.5">
            Coming soon — not yet implemented
          </span>
        </div>
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
        </section>
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
