"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

interface AuditLog {
  log_id: string;
  user_id: string | null;
  username: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  description: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  hostname: string | null;
  created_at: string | null;
}

interface AuditListResponse {
  logs: AuditLog[];
  total: number;
  offset: number;
  limit: number;
}

interface FilterOptions {
  actions: string[];
  resource_types: string[];
}

const PAGE_SIZE = 50;

function formatTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function AuditLogPage() {
  const role = getUserRole();
  const admin = isAdmin(role);

  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const [action, setAction] = useState<string>("");
  const [resourceType, setResourceType] = useState<string>("");
  const [startDate, setStartDate] = useState<string>("");
  const [endDate, setEndDate] = useState<string>("");

  const [options, setOptions] = useState<FilterOptions>({
    actions: [],
    resource_types: [],
  });

  const load = useCallback(
    async (nextOffset: number) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        params.set("offset", String(nextOffset));
        params.set("limit", String(PAGE_SIZE));
        if (action) params.set("action", action);
        if (resourceType) params.set("resource_type", resourceType);
        if (startDate) params.set("start", new Date(startDate).toISOString());
        if (endDate) params.set("end", new Date(endDate).toISOString());
        const res = await fetch(`/api/audit?${params.toString()}`, {
          credentials: "include",
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = (await res.json()) as AuditListResponse;
        setLogs(data.logs);
        setTotal(data.total);
        setOffset(data.offset);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load logs");
      } finally {
        setLoading(false);
      }
    },
    [action, resourceType, startDate, endDate],
  );

  useEffect(() => {
    if (!admin) return;
    load(0);
  }, [admin, load]);

  useEffect(() => {
    if (!admin) return;
    (async () => {
      try {
        const res = await fetch("/api/audit/actions", { credentials: "include" });
        if (!res.ok) return;
        const data = (await res.json()) as FilterOptions;
        setOptions(data);
      } catch {
        // silent
      }
    })();
  }, [admin]);

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const hasFilter = useMemo(
    () => action || resourceType || startDate || endDate,
    [action, resourceType, startDate, endDate],
  );

  const resetFilters = () => {
    setAction("");
    setResourceType("");
    setStartDate("");
    setEndDate("");
  };

  if (!admin) {
    return (
      <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 rounded-lg p-4 text-sm">
        This page is admin-only.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Audit Log</h1>
        <span className="text-xs text-gray-500">
          {total.toLocaleString()} entries
        </span>
      </div>

      <p className="text-sm text-gray-600">
        Every administrative action — manual purges, quota changes, settings
        edits, and the storage watchdog&rsquo;s automatic cleanups — is recorded
        here with who did it, when, and from where.
      </p>

      {/* Filters */}
      <div className="bg-white border border-gray-200 rounded-lg p-4 flex flex-wrap gap-3 items-end">
        <div>
          <label className="block text-xs text-gray-500 uppercase tracking-wide mb-1">
            Action
          </label>
          <select
            value={action}
            onChange={(e) => setAction(e.target.value)}
            className="text-sm border border-gray-300 rounded px-2 py-1 bg-white min-w-[140px]"
          >
            <option value="">All actions</option>
            {options.actions.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs text-gray-500 uppercase tracking-wide mb-1">
            Resource
          </label>
          <select
            value={resourceType}
            onChange={(e) => setResourceType(e.target.value)}
            className="text-sm border border-gray-300 rounded px-2 py-1 bg-white min-w-[140px]"
          >
            <option value="">All resources</option>
            {options.resource_types.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs text-gray-500 uppercase tracking-wide mb-1">
            From
          </label>
          <input
            type="datetime-local"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className="text-sm border border-gray-300 rounded px-2 py-1"
          />
        </div>

        <div>
          <label className="block text-xs text-gray-500 uppercase tracking-wide mb-1">
            To
          </label>
          <input
            type="datetime-local"
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
            className="text-sm border border-gray-300 rounded px-2 py-1"
          />
        </div>

        <button
          onClick={() => load(0)}
          className="text-sm px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded"
        >
          Apply
        </button>
        {hasFilter && (
          <button
            onClick={() => {
              resetFilters();
              // load on next effect isn't guaranteed synchronous; force
              setTimeout(() => load(0), 0);
            }}
            className="text-sm px-3 py-1.5 border border-gray-300 rounded hover:bg-gray-50"
          >
            Clear
          </button>
        )}
      </div>

      {/* Table */}
      {error ? (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded p-3 text-sm">
          {error}
        </div>
      ) : (
        <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-xs uppercase tracking-wide text-gray-500">
                <tr>
                  <th className="px-3 py-2 text-left">Time</th>
                  <th className="px-3 py-2 text-left">User</th>
                  <th className="px-3 py-2 text-left">Action</th>
                  <th className="px-3 py-2 text-left">Resource</th>
                  <th className="px-3 py-2 text-left">Description</th>
                  <th className="px-3 py-2 text-left">IP</th>
                  <th className="px-3 py-2 text-left">Host</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={7} className="px-3 py-6 text-center text-gray-400">
                      Loading…
                    </td>
                  </tr>
                ) : logs.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="px-3 py-6 text-center text-gray-400">
                      No audit entries match the current filters.
                    </td>
                  </tr>
                ) : (
                  logs.map((log) => {
                    const isOpen = expanded === log.log_id;
                    const resource = log.resource_id
                      ? `${log.resource_type}/${log.resource_id}`
                      : log.resource_type;
                    const summary =
                      log.description ??
                      (typeof log.details.path === "string"
                        ? (log.details.path as string)
                        : "—");
                    const actionClass =
                      log.action === "PURGE" || log.action === "AUTO_PURGE"
                        ? "bg-red-100 text-red-800"
                        : log.action === "UPDATE"
                          ? "bg-amber-100 text-amber-800"
                          : log.action === "DELETE"
                            ? "bg-red-50 text-red-700"
                            : "bg-gray-100 text-gray-700";
                    return (
                      <Fragment key={log.log_id}>
                        <tr
                          onClick={() => setExpanded(isOpen ? null : log.log_id)}
                          className="border-t border-gray-100 hover:bg-gray-50 cursor-pointer"
                        >
                          <td className="px-3 py-2 whitespace-nowrap text-gray-700">
                            {formatTime(log.created_at)}
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap">
                            {log.username ?? (
                              <span className="text-gray-400 italic">system</span>
                            )}
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap">
                            <span
                              className={`inline-block rounded px-2 py-0.5 text-xs font-mono ${actionClass}`}
                            >
                              {log.action}
                            </span>
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap font-mono text-xs text-gray-600">
                            {resource}
                          </td>
                          <td className="px-3 py-2 text-gray-700">{summary}</td>
                          <td className="px-3 py-2 whitespace-nowrap font-mono text-xs text-gray-500">
                            {log.ip_address ?? "—"}
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap font-mono text-xs text-gray-500">
                            {log.hostname ?? "—"}
                          </td>
                        </tr>
                        {isOpen && (
                          <tr className="border-t border-gray-100 bg-gray-50">
                            <td colSpan={7} className="px-3 py-3">
                              <pre className="text-xs text-gray-700 whitespace-pre-wrap break-all">
                                {JSON.stringify(log.details, null, 2)}
                              </pre>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Pagination */}
      <div className="flex items-center justify-between text-sm text-gray-600">
        <button
          onClick={() => load(Math.max(0, offset - PAGE_SIZE))}
          disabled={offset === 0 || loading}
          className="px-3 py-1 border border-gray-300 rounded disabled:opacity-40 hover:bg-gray-50"
        >
          ← Previous
        </button>
        <span>
          Page {page} of {totalPages}
        </span>
        <button
          onClick={() => load(offset + PAGE_SIZE)}
          disabled={offset + PAGE_SIZE >= total || loading}
          className="px-3 py-1 border border-gray-300 rounded disabled:opacity-40 hover:bg-gray-50"
        >
          Next →
        </button>
      </div>
    </div>
  );
}
