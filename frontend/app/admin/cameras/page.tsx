"use client";

import { useState, useEffect, useCallback } from "react";
import {
  getTopologyGraph,
  addCamera,
  removeCamera,
} from "@/lib/api-client";
import type { CameraNode, CameraCreateRequest, TopologyGraph } from "@/lib/api-client";
import { getUserRole, isAdmin } from "@/lib/auth";
import CameraForm from "@/components/CameraForm";

const DEFAULT_SITE = "00000000-0000-0000-0000-000000000001";

export default function CamerasPage() {
  const role = getUserRole();
  const [topology, setTopology] = useState<TopologyGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editCamera, setEditCamera] = useState<CameraNode | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getTopologyGraph(DEFAULT_SITE);
      setTopology(data);
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

  async function handleAdd(data: CameraCreateRequest) {
    try {
      await addCamera(DEFAULT_SITE, data);
      setShowForm(false);
      setEditCamera(null);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to add camera");
    }
  }

  async function handleDelete(cameraId: string) {
    try {
      await removeCamera(DEFAULT_SITE, cameraId);
      setDeleteConfirm(null);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete camera");
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Camera Management</h1>
        <button
          onClick={() => { setShowForm(true); setEditCamera(null); }}
          className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
        >
          Add Camera
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {(showForm || editCamera) && (
        <CameraForm
          initial={editCamera ?? undefined}
          onSubmit={handleAdd}
          onCancel={() => { setShowForm(false); setEditCamera(null); }}
        />
      )}

      {loading && <p className="text-sm text-gray-500">Loading cameras...</p>}

      {!loading && topology && (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Camera ID</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Name</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Zone</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Status</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Location</th>
                <th className="text-right px-4 py-2 font-medium text-gray-600">Actions</th>
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
                  <td className="px-4 py-2 text-gray-500 text-xs">
                    {cam.location_description ?? "-"}
                  </td>
                  <td className="px-4 py-2 text-right space-x-2">
                    <button
                      onClick={() => { setEditCamera(cam); setShowForm(false); }}
                      className="text-xs text-blue-600 hover:underline"
                    >
                      Edit
                    </button>
                    {deleteConfirm === cam.camera_id ? (
                      <>
                        <span className="text-xs text-gray-500">Confirm?</span>
                        <button
                          onClick={() => handleDelete(cam.camera_id)}
                          className="text-xs text-red-600 hover:underline"
                        >
                          Yes
                        </button>
                        <button
                          onClick={() => setDeleteConfirm(null)}
                          className="text-xs text-gray-500 hover:underline"
                        >
                          No
                        </button>
                      </>
                    ) : (
                      <button
                        onClick={() => setDeleteConfirm(cam.camera_id)}
                        className="text-xs text-red-600 hover:underline"
                      >
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {topology.cameras.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-gray-400">
                    No cameras configured. Click &ldquo;Add Camera&rdquo; to get started.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
