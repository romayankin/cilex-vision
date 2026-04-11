"use client";

import type { HealthStatus } from "./SiteHealthCard";

export interface SiteOption {
  siteId: string;
  name: string;
  status: HealthStatus;
}

interface SiteSwitcherProps {
  sites: SiteOption[];
  currentSiteId: string | null;
  onChange: (siteId: string) => void;
}

const DOT_COLOR: Record<HealthStatus, string> = {
  healthy: "bg-green-500",
  warning: "bg-yellow-500",
  critical: "bg-red-500",
};

export default function SiteSwitcher({ sites, currentSiteId, onChange }: SiteSwitcherProps) {
  const current = sites.find((s) => s.siteId === currentSiteId);

  return (
    <div className="relative inline-flex items-center">
      {current && (
        <span className={`inline-block w-2 h-2 rounded-full mr-1.5 ${DOT_COLOR[current.status]}`} />
      )}
      <select
        value={currentSiteId ?? ""}
        onChange={(e) => onChange(e.target.value)}
        className="appearance-none bg-transparent text-sm font-medium text-gray-700 pr-6 py-1 cursor-pointer hover:text-gray-900 focus:outline-none"
      >
        {sites.map((s) => (
          <option key={s.siteId} value={s.siteId}>
            {s.name}
          </option>
        ))}
      </select>
      <svg
        className="absolute right-0 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
      </svg>
    </div>
  );
}
