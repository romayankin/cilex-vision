"use client";

import { useState } from "react";
import type { CameraNode, CameraCreateRequest } from "@/lib/api-client";

interface CameraFormProps {
  initial?: CameraNode;
  onSubmit: (data: CameraCreateRequest) => void;
  onCancel: () => void;
}

export default function CameraForm({ initial, onSubmit, onCancel }: CameraFormProps) {
  const [cameraId, setCameraId] = useState(initial?.camera_id ?? "");
  const [name, setName] = useState(initial?.name ?? "");
  const [zoneId, setZoneId] = useState(initial?.zone_id ?? "");
  const [latitude, setLatitude] = useState(initial?.latitude?.toString() ?? "");
  const [longitude, setLongitude] = useState(initial?.longitude?.toString() ?? "");
  const [locationDescription, setLocationDescription] = useState(
    initial?.location_description ?? ""
  );

  const isEdit = !!initial;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!cameraId.trim() || !name.trim()) return;

    const data: CameraCreateRequest = {
      camera_id: cameraId.trim(),
      name: name.trim(),
    };
    if (zoneId.trim()) data.zone_id = zoneId.trim();
    if (latitude) data.latitude = parseFloat(latitude);
    if (longitude) data.longitude = parseFloat(longitude);
    if (locationDescription.trim()) data.location_description = locationDescription.trim();

    onSubmit(data);
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4 bg-white border border-gray-200 rounded-lg p-4">
      <h3 className="font-medium text-sm">{isEdit ? "Edit Camera" : "Add Camera"}</h3>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="block">
          <span className="text-xs text-gray-600">Camera ID *</span>
          <input
            type="text"
            value={cameraId}
            onChange={(e) => setCameraId(e.target.value)}
            disabled={isEdit}
            required
            className="mt-1 block w-full rounded border border-gray-300 px-2 py-1.5 text-sm disabled:bg-gray-100"
            placeholder="cam-lobby-01"
          />
        </label>

        <label className="block">
          <span className="text-xs text-gray-600">Name *</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            className="mt-1 block w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            placeholder="Lobby Camera 1"
          />
        </label>

        <label className="block">
          <span className="text-xs text-gray-600">Zone ID</span>
          <input
            type="text"
            value={zoneId}
            onChange={(e) => setZoneId(e.target.value)}
            className="mt-1 block w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            placeholder="zone-a"
          />
        </label>

        <label className="block">
          <span className="text-xs text-gray-600">Location Description</span>
          <input
            type="text"
            value={locationDescription}
            onChange={(e) => setLocationDescription(e.target.value)}
            className="mt-1 block w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
            placeholder="Main entrance, facing south"
          />
        </label>

        <label className="block">
          <span className="text-xs text-gray-600">Latitude</span>
          <input
            type="number"
            step="any"
            value={latitude}
            onChange={(e) => setLatitude(e.target.value)}
            className="mt-1 block w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
          />
        </label>

        <label className="block">
          <span className="text-xs text-gray-600">Longitude</span>
          <input
            type="number"
            step="any"
            value={longitude}
            onChange={(e) => setLongitude(e.target.value)}
            className="mt-1 block w-full rounded border border-gray-300 px-2 py-1.5 text-sm"
          />
        </label>
      </div>

      <div className="flex gap-2 pt-2">
        <button
          type="submit"
          className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700"
        >
          {isEdit ? "Update" : "Add Camera"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1.5 bg-gray-100 text-gray-700 text-sm rounded hover:bg-gray-200"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
