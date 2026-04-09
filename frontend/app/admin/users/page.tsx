"use client";

import { getUserRole, isAdmin } from "@/lib/auth";

const ROLES = [
  {
    role: "admin",
    desc: "Full system access. Camera CRUD, topology editing, retention config, user management, all data.",
  },
  {
    role: "operator",
    desc: "Operational monitoring. Read-only topology, event triage, camera health. No config changes.",
  },
  {
    role: "viewer",
    desc: "Read-only access to events, tracks, and detections scoped to assigned cameras.",
  },
  {
    role: "engineering",
    desc: "Debug traces, model metrics, calibration data. No event or clip access.",
  },
];

const PERMISSIONS = [
  { action: "View detections", admin: true, operator: true, viewer: true, engineering: true },
  { action: "View tracks", admin: true, operator: true, viewer: true, engineering: true },
  { action: "View events", admin: true, operator: true, viewer: true, engineering: false },
  { action: "View event clips", admin: true, operator: true, viewer: true, engineering: false },
  { action: "View debug traces", admin: true, operator: false, viewer: false, engineering: true },
  { action: "Read topology", admin: true, operator: true, viewer: false, engineering: false },
  { action: "Edit topology", admin: true, operator: false, viewer: false, engineering: false },
  { action: "Manage cameras", admin: true, operator: false, viewer: false, engineering: false },
  { action: "Change retention", admin: true, operator: false, viewer: false, engineering: false },
  { action: "Manage users", admin: true, operator: false, viewer: false, engineering: false },
  { action: "Run DSAR export", admin: true, operator: false, viewer: false, engineering: false },
  { action: "Run deletion job", admin: true, operator: false, viewer: false, engineering: false },
];

function Check({ allowed }: { allowed: boolean }) {
  return allowed ? (
    <span className="text-green-600 font-medium">Y</span>
  ) : (
    <span className="text-gray-300">-</span>
  );
}

export default function UsersPage() {
  const role = getUserRole();

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Users &amp; Roles</h1>

      <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 text-xs rounded p-3">
        User CRUD requires an auth service not yet built. This page shows role definitions and the permission matrix.
      </div>

      {/* Role definitions */}
      <section>
        <h2 className="text-lg font-medium mb-3">Role Definitions</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {ROLES.map((r) => (
            <div key={r.role} className="bg-white border border-gray-200 rounded-lg p-4">
              <h3 className="font-medium text-sm capitalize">{r.role}</h3>
              <p className="text-xs text-gray-500 mt-1">{r.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Permission matrix */}
      <section>
        <h2 className="text-lg font-medium mb-3">Permission Matrix</h2>
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-2 font-medium text-gray-600">Action</th>
                <th className="text-center px-4 py-2 font-medium text-gray-600">Admin</th>
                <th className="text-center px-4 py-2 font-medium text-gray-600">Operator</th>
                <th className="text-center px-4 py-2 font-medium text-gray-600">Viewer</th>
                <th className="text-center px-4 py-2 font-medium text-gray-600">Engineering</th>
              </tr>
            </thead>
            <tbody>
              {PERMISSIONS.map((p) => (
                <tr key={p.action} className="border-b border-gray-100">
                  <td className="px-4 py-2">{p.action}</td>
                  <td className="px-4 py-2 text-center"><Check allowed={p.admin} /></td>
                  <td className="px-4 py-2 text-center"><Check allowed={p.operator} /></td>
                  <td className="px-4 py-2 text-center"><Check allowed={p.viewer} /></td>
                  <td className="px-4 py-2 text-center"><Check allowed={p.engineering} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
