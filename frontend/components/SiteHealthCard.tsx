"use client";

export type HealthStatus = "healthy" | "warning" | "critical";

interface SiteHealthCardProps {
  siteId: string;
  name: string;
  cameraCount: number;
  activeAlerts: number;
  storagePct: number;
  status: HealthStatus;
  onClick?: () => void;
}

const STATUS_STYLES: Record<HealthStatus, { border: string; badge: string; dot: string }> = {
  healthy: {
    border: "border-green-300 hover:border-green-400",
    badge: "bg-green-100 text-green-700",
    dot: "bg-green-500",
  },
  warning: {
    border: "border-yellow-300 hover:border-yellow-400",
    badge: "bg-yellow-100 text-yellow-700",
    dot: "bg-yellow-500",
  },
  critical: {
    border: "border-red-300 hover:border-red-400",
    badge: "bg-red-100 text-red-700",
    dot: "bg-red-500",
  },
};

export default function SiteHealthCard({
  siteId,
  name,
  cameraCount,
  activeAlerts,
  storagePct,
  status,
  onClick,
}: SiteHealthCardProps) {
  const styles = STATUS_STYLES[status];

  return (
    <button
      onClick={onClick}
      className={`w-full text-left bg-white border ${styles.border} rounded-lg p-4 hover:shadow-md transition-all`}
    >
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-medium text-sm text-gray-900 truncate">{name}</h3>
        <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${styles.badge}`}>
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${styles.dot}`} />
          {status}
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 text-xs">
        <div>
          <div className="text-gray-500">Cameras</div>
          <div className="font-medium text-gray-900">{cameraCount}</div>
        </div>
        <div>
          <div className="text-gray-500">Alerts</div>
          <div className={`font-medium ${activeAlerts > 0 ? "text-red-600" : "text-gray-900"}`}>
            {activeAlerts}
          </div>
        </div>
        <div>
          <div className="text-gray-500">Storage</div>
          <div className="font-medium text-gray-900">{storagePct}%</div>
        </div>
      </div>

      <div className="mt-2 w-full bg-gray-200 rounded-full h-1.5">
        <div
          className={`h-1.5 rounded-full ${
            storagePct > 90 ? "bg-red-500" : storagePct > 70 ? "bg-yellow-500" : "bg-green-500"
          }`}
          style={{ width: `${Math.min(storagePct, 100)}%` }}
        />
      </div>

      <div className="mt-2 text-xs text-gray-400 font-mono">{siteId}</div>
    </button>
  );
}
