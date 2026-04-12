"use client";

import { useState, useEffect, useCallback } from "react";
import {
  getTopologyGraph,
  upsertEdge,
} from "@/lib/api-client";
import type { TopologyGraph, CameraNode } from "@/lib/api-client";
import { getUserRole, isAdmin } from "@/lib/auth";
import TopologyEditor from "@/components/TopologyEditor";

const DEFAULT_SITE = "00000000-0000-0000-0000-000000000001";

export default function TopologyPage() {
  const role = getUserRole();
  const [topology, setTopology] = useState<TopologyGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedCamera, setSelectedCamera] = useState<CameraNode | null>(null);
  const [edgeModal, setEdgeModal] = useState<{ a: string; b: string } | null>(null);
  const [transitTime, setTransitTime] = useState("10");

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getTopologyGraph(DEFAULT_SITE);
      setTopology(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load topology");
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

  function handleAddEdge(cameraA: string, cameraB: string) {
    setEdgeModal({ a: cameraA, b: cameraB });
    setTransitTime("10");
  }

  async function submitEdge() {
    if (!edgeModal) return;
    try {
      await upsertEdge(DEFAULT_SITE, {
        camera_a_id: edgeModal.a,
        camera_b_id: edgeModal.b,
        transition_time_s: parseFloat(transitTime) || 10,
      });
      setEdgeModal(null);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save edge");
    }
  }

  async function handleRemoveEdge(cameraA: string, cameraB: string) {
    // Disable by setting enabled=false (no DELETE endpoint for edges)
    try {
      const existing = topology?.edges.find(
        (e) => e.camera_a_id === cameraA && e.camera_b_id === cameraB
      );
      await upsertEdge(DEFAULT_SITE, {
        camera_a_id: cameraA,
        camera_b_id: cameraB,
        transition_time_s: existing?.transition_time_s ?? 10,
        enabled: false,
      });
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update edge");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Topology Editor</h1>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {loading && <p className="text-sm text-gray-500">Loading topology...</p>}

      {!loading && topology && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2">
            <TopologyEditor
              topology={topology}
              onAddEdge={handleAddEdge}
              onRemoveEdge={handleRemoveEdge}
              onSelectCamera={setSelectedCamera}
            />
          </div>

          {/* Camera detail panel */}
          <div className="bg-white border border-gray-200 rounded-lg p-4">
            <h2 className="font-medium text-sm mb-3">Camera Details</h2>
            {selectedCamera ? (
              <dl className="space-y-2 text-sm">
                <div>
                  <dt className="text-gray-500 text-xs">Camera ID</dt>
                  <dd className="font-mono text-xs">{selectedCamera.camera_id}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 text-xs">Name</dt>
                  <dd>{selectedCamera.name}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 text-xs">Status</dt>
                  <dd>
                    <span
                      className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                        selectedCamera.status === "online"
                          ? "bg-green-100 text-green-700"
                          : "bg-red-100 text-red-700"
                      }`}
                    >
                      {selectedCamera.status}
                    </span>
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 text-xs">Zone</dt>
                  <dd>{selectedCamera.zone_id ?? "None"}</dd>
                </div>
                {selectedCamera.location_description && (
                  <div>
                    <dt className="text-gray-500 text-xs">Location</dt>
                    <dd>{selectedCamera.location_description}</dd>
                  </div>
                )}
                {selectedCamera.latitude != null && (
                  <div>
                    <dt className="text-gray-500 text-xs">Coordinates</dt>
                    <dd className="font-mono text-xs">
                      {selectedCamera.latitude}, {selectedCamera.longitude}
                    </dd>
                  </div>
                )}
              </dl>
            ) : (
              <p className="text-xs text-gray-400">Click a camera node to view details.</p>
            )}
          </div>
        </div>
      )}

      {/* Edge creation modal */}
      {edgeModal && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 shadow-lg w-96 space-y-4">
            <h3 className="font-medium text-sm">Create Edge</h3>
            <p className="text-xs text-gray-500">
              {edgeModal.a} &rarr; {edgeModal.b}
            </p>
            <label className="block">
              <span className="text-xs text-gray-600">Transit Time (seconds)</span>
              <input
                type="number"
                min="1"
                step="1"
                value={transitTime}
                onChange={(e) => setTransitTime(e.target.value)}
                className="mt-1 block w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
              />
            </label>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setEdgeModal(null)}
                className="px-3 py-1.5 bg-gray-100 text-gray-700 text-sm rounded hover:bg-gray-200"
              >
                Cancel
              </button>
              <button
                onClick={submitEdge}
                className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
              >
                Save Edge
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
