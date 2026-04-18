"use client";

import { useState, useEffect, useCallback } from "react";
import {
  getTopologyGraph,
  addCamera,
  removeCamera,
  getCameraProfiles,
  getProfileAssignments,
  assignCameraProfile,
  type CameraProfile,
  type ProfileAssignment,
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
  const [profiles, setProfiles] = useState<CameraProfile[]>([]);
  const [assignments, setAssignments] = useState<Record<string, ProfileAssignment>>({});
  const [pendingAssign, setPendingAssign] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const [topo, profs, asgn] = await Promise.all([
        getTopologyGraph(DEFAULT_SITE),
        getCameraProfiles().catch(() => ({ profiles: [] as CameraProfile[] })),
        getProfileAssignments().catch(() => ({ assignments: [] as ProfileAssignment[] })),
      ]);
      setTopology(topo);
      setProfiles(profs.profiles);
      const map: Record<string, ProfileAssignment> = {};
      for (const a of asgn.assignments) map[a.camera_id] = a;
      setAssignments(map);
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

  async function handleProfileChange(cameraId: string, profileId: string) {
    try {
      setPendingAssign(cameraId);
      await assignCameraProfile(cameraId, profileId);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to assign profile");
    } finally {
      setPendingAssign(null);
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
                <th className="text-left px-4 py-2 font-medium text-gray-600">Recording Profile</th>
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
                  <td className="px-4 py-2 text-xs">
                    <select
                      value={assignments[cam.camera_id]?.profile_id ?? ""}
                      onChange={(e) =>
                        handleProfileChange(cam.camera_id, e.target.value)
                      }
                      disabled={
                        profiles.length === 0 || pendingAssign === cam.camera_id
                      }
                      className="border border-gray-300 rounded px-1.5 py-0.5 text-xs bg-white disabled:bg-gray-50"
                    >
                      {!assignments[cam.camera_id]?.profile_id && (
                        <option value="">— none —</option>
                      )}
                      {profiles.map((p) => (
                        <option key={p.profile_id} value={p.profile_id}>
                          {p.name}
                          {p.is_default ? " (default)" : ""}
                        </option>
                      ))}
                    </select>
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
                  <td colSpan={7} className="px-4 py-6 text-center text-gray-400">
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
