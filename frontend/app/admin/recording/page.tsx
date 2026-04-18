"use client";

import { useCallback, useEffect, useState } from "react";
import { Video, Plus, Pencil, Trash2, Clock, Star } from "lucide-react";
import {
  getCameraProfiles,
  createCameraProfile,
  updateCameraProfile,
  deleteCameraProfile,
  type CameraProfile,
  type CameraProfileCreate,
} from "@/lib/api-client";
import { getUserRole, isAdmin } from "@/lib/auth";

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const TIMEZONES = [
  "UTC",
  "Europe/Moscow",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "America/Los_Angeles",
  "Asia/Dubai",
  "Asia/Tokyo",
];

function emptyProfile(): CameraProfileCreate {
  return {
    name: "",
    description: "",
    recording_mode: "continuous",
    business_hours_start: "09:00:00",
    business_hours_end: "18:00:00",
    business_days: [1, 2, 3, 4, 5],
    motion_sensitivity: 0.5,
    pre_roll_s: 5,
    post_roll_s: 5,
    timezone: "UTC",
  };
}

function normalizeTime(value: string | null | undefined): string {
  if (!value) return "";
  // backend returns "HH:MM:SS", <input type="time"> wants "HH:MM"
  return value.length >= 5 ? value.slice(0, 5) : value;
}

function ModeBadge({ mode }: { mode: string }) {
  const color =
    mode === "continuous"
      ? "bg-blue-100 text-blue-700"
      : mode === "motion"
      ? "bg-amber-100 text-amber-700"
      : "bg-purple-100 text-purple-700";
  const label =
    mode === "continuous" ? "24/7" : mode === "motion" ? "Motion" : "Hybrid";
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${color}`}>
      {label}
    </span>
  );
}

export default function RecordingProfilesPage() {
  const role = getUserRole();
  const [profiles, setProfiles] = useState<CameraProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<CameraProfile | null>(null);
  const [form, setForm] = useState<CameraProfileCreate | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getCameraProfiles();
      setProfiles(data.profiles);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load profiles");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  function openCreate() {
    setEditing(null);
    setForm(emptyProfile());
  }

  function openEdit(p: CameraProfile) {
    setEditing(p);
    setForm({
      name: p.name,
      description: p.description,
      recording_mode: p.recording_mode,
      business_hours_start: normalizeTime(p.business_hours_start),
      business_hours_end: normalizeTime(p.business_hours_end),
      business_days: p.business_days ?? [1, 2, 3, 4, 5],
      motion_sensitivity: p.motion_sensitivity,
      pre_roll_s: p.pre_roll_s,
      post_roll_s: p.post_roll_s,
      timezone: p.timezone,
    });
  }

  function closeForm() {
    setForm(null);
    setEditing(null);
  }

  async function save() {
    if (!form) return;
    try {
      setSaving(true);
      setError(null);
      const payload: CameraProfileCreate = {
        ...form,
        description: form.description || null,
        business_hours_start:
          form.recording_mode === "hybrid" ? form.business_hours_start : null,
        business_hours_end:
          form.recording_mode === "hybrid" ? form.business_hours_end : null,
      };
      if (editing) {
        await updateCameraProfile(editing.profile_id, payload);
      } else {
        await createCameraProfile(payload);
      }
      closeForm();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save profile");
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete(profileId: string) {
    try {
      await deleteCameraProfile(profileId);
      setDeleteConfirm(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete profile");
    }
  }

  return (
    <div className="space-y-4 pb-8">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="bg-blue-50 text-blue-600 rounded-lg p-2">
            <Video className="w-5 h-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold text-gray-900">Recording Profiles</h1>
            <p className="text-xs text-gray-500">
              Reusable recording configurations applied per-camera.
            </p>
          </div>
        </div>
        <button
          onClick={openCreate}
          className="inline-flex items-center gap-1 px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
        >
          <Plus className="w-4 h-4" />
          New Profile
        </button>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded p-3">
          {error}
        </div>
      )}

      {loading && <p className="text-sm text-gray-500">Loading profiles…</p>}

      {!loading && (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Name</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Mode</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Schedule</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Sensitivity</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Pre/Post roll</th>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Cameras</th>
                <th className="text-right px-4 py-2 font-medium text-gray-600">Actions</th>
              </tr>
            </thead>
            <tbody>
              {profiles.map((p) => {
                const scheduleText =
                  p.recording_mode === "hybrid"
                    ? `${normalizeTime(p.business_hours_start) || "--:--"}–${
                        normalizeTime(p.business_hours_end) || "--:--"
                      } (${(p.business_days ?? [])
                        .sort((a, b) => a - b)
                        .map((d) => DAY_LABELS[(d - 1) % 7])
                        .join(",")})`
                    : p.recording_mode === "continuous"
                    ? "Always"
                    : "Event-driven";
                return (
                  <tr
                    key={p.profile_id}
                    className="border-b border-gray-100 hover:bg-gray-50"
                  >
                    <td className="px-4 py-2">
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{p.name}</span>
                        {p.is_default && (
                          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-yellow-100 text-yellow-700 text-xs">
                            <Star className="w-3 h-3" />
                            Default
                          </span>
                        )}
                      </div>
                      {p.description && (
                        <div className="text-xs text-gray-500 mt-0.5">
                          {p.description}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      <ModeBadge mode={p.recording_mode} />
                    </td>
                    <td className="px-4 py-2 text-xs text-gray-600">
                      <div className="flex items-center gap-1">
                        <Clock className="w-3 h-3 text-gray-400" />
                        {scheduleText}
                      </div>
                      <div className="text-xs text-gray-400 mt-0.5">
                        TZ: {p.timezone}
                      </div>
                    </td>
                    <td className="px-4 py-2 text-xs text-gray-600">
                      {p.motion_sensitivity.toFixed(2)}
                    </td>
                    <td className="px-4 py-2 text-xs text-gray-600">
                      {p.pre_roll_s}s / {p.post_roll_s}s
                    </td>
                    <td className="px-4 py-2 text-xs">
                      <span className="font-mono">{p.cameras_assigned}</span>
                    </td>
                    <td className="px-4 py-2 text-right space-x-2">
                      <button
                        onClick={() => openEdit(p)}
                        className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
                      >
                        <Pencil className="w-3 h-3" />
                        Edit
                      </button>
                      {deleteConfirm === p.profile_id ? (
                        <>
                          <span className="text-xs text-gray-500">Confirm?</span>
                          <button
                            onClick={() => confirmDelete(p.profile_id)}
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
                          onClick={() => setDeleteConfirm(p.profile_id)}
                          disabled={p.is_default || p.cameras_assigned > 0}
                          className="inline-flex items-center gap-1 text-xs text-red-600 hover:underline disabled:text-gray-300 disabled:cursor-not-allowed disabled:no-underline"
                          title={
                            p.is_default
                              ? "Cannot delete the default profile"
                              : p.cameras_assigned > 0
                              ? `Assigned to ${p.cameras_assigned} cameras`
                              : "Delete"
                          }
                        >
                          <Trash2 className="w-3 h-3" />
                          Delete
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
              {profiles.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-gray-400">
                    No profiles defined.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {form && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-lg shadow-lg w-full max-w-2xl max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between px-5 py-3 border-b">
              <h2 className="font-semibold">
                {editing ? `Edit: ${editing.name}` : "New Recording Profile"}
              </h2>
              <button
                onClick={closeForm}
                className="text-gray-400 hover:text-gray-700 text-xl leading-none"
                aria-label="Close"
              >
                ×
              </button>
            </div>

            <div className="p-5 space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <label className="block text-sm">
                  <span className="text-gray-700 font-medium">Name</span>
                  <input
                    type="text"
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
                    required
                  />
                </label>
                <label className="block text-sm">
                  <span className="text-gray-700 font-medium">Timezone</span>
                  <select
                    value={form.timezone}
                    onChange={(e) =>
                      setForm({ ...form, timezone: e.target.value })
                    }
                    className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
                  >
                    {TIMEZONES.map((tz) => (
                      <option key={tz} value={tz}>
                        {tz}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <label className="block text-sm">
                <span className="text-gray-700 font-medium">Description</span>
                <input
                  type="text"
                  value={form.description ?? ""}
                  onChange={(e) =>
                    setForm({ ...form, description: e.target.value })
                  }
                  className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
                />
              </label>

              <fieldset className="border border-gray-200 rounded p-3">
                <legend className="text-sm font-medium text-gray-700 px-1">
                  Recording mode
                </legend>
                <div className="flex flex-col gap-2 text-sm mt-1">
                  {(["continuous", "motion", "hybrid"] as const).map((mode) => (
                    <label key={mode} className="flex items-start gap-2">
                      <input
                        type="radio"
                        checked={form.recording_mode === mode}
                        onChange={() =>
                          setForm({ ...form, recording_mode: mode })
                        }
                        className="mt-0.5"
                      />
                      <span>
                        <span className="font-medium capitalize">
                          {mode === "continuous" ? "24/7 continuous" : mode}
                        </span>
                        <span className="block text-xs text-gray-500">
                          {mode === "continuous" &&
                            "Always recording. Highest storage cost."}
                          {mode === "motion" &&
                            "Record only around motion events (not yet active — falls back to 24/7)."}
                          {mode === "hybrid" &&
                            "Continuous during business hours, motion outside (not yet active — falls back to 24/7)."}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>

              {form.recording_mode === "hybrid" && (
                <div className="border border-gray-200 rounded p-3 space-y-3 bg-purple-50/40">
                  <div className="grid grid-cols-2 gap-3">
                    <label className="block text-sm">
                      <span className="text-gray-700 font-medium">
                        Business hours start
                      </span>
                      <input
                        type="time"
                        value={normalizeTime(form.business_hours_start)}
                        onChange={(e) =>
                          setForm({
                            ...form,
                            business_hours_start: e.target.value + ":00",
                          })
                        }
                        className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
                      />
                    </label>
                    <label className="block text-sm">
                      <span className="text-gray-700 font-medium">
                        Business hours end
                      </span>
                      <input
                        type="time"
                        value={normalizeTime(form.business_hours_end)}
                        onChange={(e) =>
                          setForm({
                            ...form,
                            business_hours_end: e.target.value + ":00",
                          })
                        }
                        className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
                      />
                    </label>
                  </div>

                  <div>
                    <span className="text-sm text-gray-700 font-medium">
                      Business days
                    </span>
                    <div className="flex gap-2 mt-1">
                      {DAY_LABELS.map((label, idx) => {
                        const dayNum = idx + 1;
                        const active = form.business_days.includes(dayNum);
                        return (
                          <button
                            key={label}
                            type="button"
                            onClick={() =>
                              setForm({
                                ...form,
                                business_days: active
                                  ? form.business_days.filter(
                                      (d) => d !== dayNum,
                                    )
                                  : [...form.business_days, dayNum].sort(
                                      (a, b) => a - b,
                                    ),
                              })
                            }
                            className={`px-2 py-1 text-xs rounded border ${
                              active
                                ? "bg-blue-600 text-white border-blue-600"
                                : "bg-white text-gray-700 border-gray-300 hover:border-blue-300"
                            }`}
                          >
                            {label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}

              <label className="block text-sm">
                <span className="text-gray-700 font-medium flex items-center justify-between">
                  Motion sensitivity
                  <span className="text-xs text-gray-500 font-mono">
                    {form.motion_sensitivity.toFixed(2)}
                  </span>
                </span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={form.motion_sensitivity}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      motion_sensitivity: parseFloat(e.target.value),
                    })
                  }
                  className="w-full mt-1"
                />
                <div className="flex justify-between text-xs text-gray-400">
                  <span>low (0.0)</span>
                  <span>high (1.0)</span>
                </div>
              </label>

              <div className="grid grid-cols-2 gap-3">
                <label className="block text-sm">
                  <span className="text-gray-700 font-medium">Pre-roll (s)</span>
                  <input
                    type="number"
                    min={0}
                    max={60}
                    value={form.pre_roll_s}
                    onChange={(e) =>
                      setForm({
                        ...form,
                        pre_roll_s: parseInt(e.target.value) || 0,
                      })
                    }
                    className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
                  />
                </label>
                <label className="block text-sm">
                  <span className="text-gray-700 font-medium">Post-roll (s)</span>
                  <input
                    type="number"
                    min={0}
                    max={60}
                    value={form.post_roll_s}
                    onChange={(e) =>
                      setForm({
                        ...form,
                        post_roll_s: parseInt(e.target.value) || 0,
                      })
                    }
                    className="mt-1 w-full border border-gray-300 rounded px-2 py-1 text-sm"
                  />
                </label>
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 px-5 py-3 border-t bg-gray-50">
              <button
                onClick={closeForm}
                className="px-3 py-1.5 text-sm rounded border border-gray-300 bg-white hover:bg-gray-50"
                disabled={saving}
              >
                Cancel
              </button>
              <button
                onClick={save}
                disabled={saving || !form.name.trim()}
                className="px-3 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:bg-gray-300"
              >
                {saving ? "Saving…" : editing ? "Save changes" : "Create profile"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
