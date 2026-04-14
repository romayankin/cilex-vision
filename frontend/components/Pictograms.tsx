"use client";

/**
 * Wireframe pictograms for search filter tiles.
 * All icons use currentColor stroke, no fill — blueprint/technical drawing style.
 * Consistent 40x40 viewBox, stroke-width 1.5.
 */

type IconProps = { className?: string };

const base = "shrink-0";

// ---------------------------------------------------------------------------
// Object class icons
// ---------------------------------------------------------------------------

export function PersonIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <circle cx="20" cy="10" r="4" />
      <line x1="20" y1="14" x2="20" y2="26" />
      <line x1="20" y1="18" x2="13" y2="22" />
      <line x1="20" y1="18" x2="27" y2="22" />
      <line x1="20" y1="26" x2="14" y2="34" />
      <line x1="20" y1="26" x2="26" y2="34" />
    </svg>
  );
}

export function CarIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <path d="M4 26 L4 22 L9 14 L27 14 L34 22 L36 22 L36 26 Z" />
      <line x1="14" y1="14" x2="14" y2="22" />
      <line x1="22" y1="14" x2="22" y2="22" />
      <line x1="4" y1="22" x2="36" y2="22" />
      <circle cx="11" cy="28" r="3" />
      <circle cx="29" cy="28" r="3" />
    </svg>
  );
}

export function TruckIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <rect x="3" y="12" width="20" height="16" />
      <path d="M23 16 L31 16 L36 22 L36 28 L23 28 Z" />
      <line x1="27" y1="16" x2="27" y2="22" />
      <line x1="23" y1="22" x2="36" y2="22" />
      <circle cx="9" cy="30" r="3" />
      <circle cx="29" cy="30" r="3" />
    </svg>
  );
}

export function BusIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <rect x="4" y="10" width="32" height="20" rx="2" />
      <line x1="4" y1="22" x2="36" y2="22" />
      <rect x="7" y="13" width="5" height="5" />
      <rect x="14" y="13" width="5" height="5" />
      <rect x="21" y="13" width="5" height="5" />
      <rect x="28" y="13" width="5" height="5" />
      <circle cx="11" cy="32" r="2.5" />
      <circle cx="29" cy="32" r="2.5" />
    </svg>
  );
}

export function BicycleIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <circle cx="10" cy="28" r="6" />
      <circle cx="30" cy="28" r="6" />
      <line x1="10" y1="28" x2="19" y2="16" />
      <line x1="19" y1="16" x2="30" y2="28" />
      <line x1="10" y1="28" x2="22" y2="28" />
      <line x1="22" y1="28" x2="19" y2="16" />
      <line x1="19" y1="16" x2="16" y2="12" />
      <line x1="14" y1="12" x2="20" y2="12" />
    </svg>
  );
}

export function MotorcycleIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <circle cx="9" cy="28" r="5.5" />
      <circle cx="31" cy="28" r="5.5" />
      <path d="M9 28 L16 20 L24 20 L31 28" />
      <rect x="16" y="20" width="8" height="4" />
      <line x1="14" y1="15" x2="20" y2="15" />
      <line x1="17" y1="15" x2="18" y2="20" />
      <line x1="31" y1="28" x2="35" y2="30" />
    </svg>
  );
}

export function AnimalIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <ellipse cx="18" cy="22" rx="11" ry="5" />
      <circle cx="30" cy="18" r="4" />
      <line x1="28" y1="14.5" x2="27" y2="11" />
      <line x1="32" y1="14.5" x2="33" y2="11" />
      <line x1="10" y1="27" x2="9" y2="33" />
      <line x1="15" y1="27" x2="15" y2="33" />
      <line x1="22" y1="27" x2="22" y2="33" />
      <line x1="27" y1="27" x2="28" y2="33" />
      <line x1="7" y1="22" x2="4" y2="20" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Event type icons
// ---------------------------------------------------------------------------

export function EnteredSceneIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <rect x="18" y="8" width="18" height="24" strokeDasharray="3 2" />
      <line x1="4" y1="20" x2="22" y2="20" />
      <polyline points="16,14 22,20 16,26" />
    </svg>
  );
}

export function ExitedSceneIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <rect x="4" y="8" width="18" height="24" strokeDasharray="3 2" />
      <line x1="18" y1="20" x2="36" y2="20" />
      <polyline points="30,14 36,20 30,26" />
    </svg>
  );
}

export function StoppedIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <circle cx="20" cy="20" r="13" />
      <rect x="14" y="14" width="12" height="12" />
    </svg>
  );
}

export function LoiteringIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <path d="M30 20 A10 10 0 1 1 20 10" />
      <polyline points="20,6 20,10 24,10" />
      <path d="M10 20 A10 10 0 1 1 20 30" />
      <polyline points="20,34 20,30 16,30" />
    </svg>
  );
}

export function MotionStartedIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <polygon points="12,8 32,20 12,32" />
    </svg>
  );
}

export function MotionEndedIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <rect x="10" y="10" width="20" height="20" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Track state icons
// ---------------------------------------------------------------------------

export function TrackNewIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <line x1="20" y1="6" x2="20" y2="34" />
      <line x1="6" y1="20" x2="34" y2="20" />
      <line x1="10" y1="10" x2="30" y2="30" />
      <line x1="30" y1="10" x2="10" y2="30" />
    </svg>
  );
}

export function TrackActiveIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <circle cx="20" cy="20" r="3" fill="currentColor" stroke="none" />
      <circle cx="20" cy="20" r="8" />
      <circle cx="20" cy="20" r="13" strokeDasharray="2 3" />
    </svg>
  );
}

export function TrackLostIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <circle cx="20" cy="20" r="13" strokeDasharray="3 2" />
      <path d="M16 16 a4 4 0 1 1 5 4 v3" />
      <line x1="20" y1="27" x2="20" y2="28" />
    </svg>
  );
}

export function TrackTerminatedIcon({ className = "" }: IconProps) {
  return (
    <svg
      viewBox="0 0 40 40"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`${base} ${className}`}
    >
      <circle cx="20" cy="20" r="13" />
      <line x1="13" y1="13" x2="27" y2="27" />
      <line x1="27" y1="13" x2="13" y2="27" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Maps for easy lookup
// ---------------------------------------------------------------------------

export const OBJECT_CLASS_ICONS: Record<string, (p: IconProps) => JSX.Element> = {
  person: PersonIcon,
  car: CarIcon,
  truck: TruckIcon,
  bus: BusIcon,
  bicycle: BicycleIcon,
  motorcycle: MotorcycleIcon,
  animal: AnimalIcon,
};

export const EVENT_TYPE_ICONS: Record<string, (p: IconProps) => JSX.Element> = {
  entered_scene: EnteredSceneIcon,
  exited_scene: ExitedSceneIcon,
  stopped: StoppedIcon,
  loitering: LoiteringIcon,
  motion_started: MotionStartedIcon,
  motion_ended: MotionEndedIcon,
};

export const TRACK_STATE_ICONS: Record<string, (p: IconProps) => JSX.Element> = {
  new: TrackNewIcon,
  active: TrackActiveIcon,
  lost: TrackLostIcon,
  terminated: TrackTerminatedIcon,
};
