"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Dial-based time picker. Two-step: hour then minute.
 * `value` and `onChange` use HH:MM 24h strings; empty string = no selection.
 */

interface ClockPickerProps {
  value: string;
  onChange: (time: string) => void;
  label?: string;
}

function toHHMM(h: number, m: number): string {
  const hh = String(h).padStart(2, "0");
  const mm = String(m).padStart(2, "0");
  return `${hh}:${mm}`;
}

function parse(value: string): { h: number; m: number } {
  const [hs, ms] = value.split(":");
  const h = Number.isFinite(Number(hs)) ? Number(hs) : 0;
  const m = Number.isFinite(Number(ms)) ? Number(ms) : 0;
  return { h: Math.max(0, Math.min(23, h)), m: Math.max(0, Math.min(59, m)) };
}

const OUTER_HOURS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
const INNER_HOURS = [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23];
const MINUTES = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55];

function polarPosition(index: number, total: number, radius: number, cx: number, cy: number) {
  // 12 positions around the circle, index 0 at top.
  const angle = (index / total) * 2 * Math.PI - Math.PI / 2;
  return {
    x: cx + radius * Math.cos(angle),
    y: cy + radius * Math.sin(angle),
  };
}

export default function ClockPicker({ value, onChange, label }: ClockPickerProps) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<"hour" | "minute">("hour");
  const { h: initH, m: initM } = parse(value || "00:00");
  const [hour, setHour] = useState(initH);
  const [minute, setMinute] = useState(initM);
  const popRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (popRef.current && !popRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  useEffect(() => {
    const p = parse(value || "00:00");
    setHour(p.h);
    setMinute(p.m);
  }, [value]);

  function pickHour(h: number) {
    setHour(h);
    setStep("minute");
  }

  function pickMinute(m: number) {
    setMinute(m);
    onChange(toHHMM(hour, m));
    setOpen(false);
    setStep("hour");
  }

  function bumpMinute(delta: number) {
    const next = (minute + delta + 60) % 60;
    setMinute(next);
    onChange(toHHMM(hour, next));
  }

  function openPicker() {
    const p = parse(value || "00:00");
    setHour(p.h);
    setMinute(p.m);
    setStep("hour");
    setOpen(true);
  }

  const display = value || "--:--";
  const size = 220;
  const cx = size / 2;
  const cy = size / 2;
  const outerR = 90;
  const innerR = 60;
  const minuteR = 90;

  return (
    <div className="relative" ref={popRef}>
      {label && <label className="block text-xs text-gray-500 mb-1">{label}</label>}
      <button
        type="button"
        onClick={openPicker}
        className="w-full border border-gray-300 rounded px-2 py-1.5 text-sm text-left bg-white hover:bg-gray-50 font-mono"
      >
        {display}
      </button>

      {open && (
        <div className="absolute z-30 mt-1 left-0 bg-white border border-gray-300 rounded-lg shadow-xl p-3">
          <div className="flex items-center justify-between mb-2 text-xs text-gray-600">
            <div className="flex gap-1">
              <button
                type="button"
                onClick={() => setStep("hour")}
                className={`px-2 py-0.5 rounded font-mono ${
                  step === "hour" ? "bg-gray-900 text-white" : "bg-gray-100"
                }`}
              >
                {String(hour).padStart(2, "0")}h
              </button>
              <button
                type="button"
                onClick={() => setStep("minute")}
                className={`px-2 py-0.5 rounded font-mono ${
                  step === "minute" ? "bg-gray-900 text-white" : "bg-gray-100"
                }`}
              >
                {String(minute).padStart(2, "0")}m
              </button>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-gray-400 hover:text-gray-700"
              aria-label="Close"
            >
              ✕
            </button>
          </div>

          <svg width={size} height={size} className="select-none">
            <circle cx={cx} cy={cy} r={outerR + 12} fill="#F9FAFB" stroke="#E5E7EB" />
            {step === "hour" ? (
              <>
                {OUTER_HOURS.map((h, i) => {
                  const { x, y } = polarPosition(i, 12, outerR, cx, cy);
                  const selected = h === hour;
                  return (
                    <g key={`o-${h}`} onClick={() => pickHour(h)} style={{ cursor: "pointer" }}>
                      <circle
                        cx={x}
                        cy={y}
                        r="13"
                        fill={selected ? "#2563EB" : "transparent"}
                      />
                      <text
                        x={x}
                        y={y + 4}
                        textAnchor="middle"
                        fontSize="12"
                        fontFamily="monospace"
                        fill={selected ? "#FFFFFF" : "#374151"}
                      >
                        {String(h).padStart(2, "0")}
                      </text>
                    </g>
                  );
                })}
                {INNER_HOURS.map((h, i) => {
                  const { x, y } = polarPosition(i, 12, innerR, cx, cy);
                  const selected = h === hour;
                  return (
                    <g key={`i-${h}`} onClick={() => pickHour(h)} style={{ cursor: "pointer" }}>
                      <circle
                        cx={x}
                        cy={y}
                        r="11"
                        fill={selected ? "#2563EB" : "transparent"}
                      />
                      <text
                        x={x}
                        y={y + 4}
                        textAnchor="middle"
                        fontSize="11"
                        fontFamily="monospace"
                        fill={selected ? "#FFFFFF" : "#6B7280"}
                      >
                        {String(h).padStart(2, "0")}
                      </text>
                    </g>
                  );
                })}
              </>
            ) : (
              MINUTES.map((m, i) => {
                const { x, y } = polarPosition(i, 12, minuteR, cx, cy);
                const selected = m === minute;
                return (
                  <g key={`m-${m}`} onClick={() => pickMinute(m)} style={{ cursor: "pointer" }}>
                    <circle
                      cx={x}
                      cy={y}
                      r="14"
                      fill={selected ? "#2563EB" : "transparent"}
                    />
                    <text
                      x={x}
                      y={y + 4}
                      textAnchor="middle"
                      fontSize="12"
                      fontFamily="monospace"
                      fill={selected ? "#FFFFFF" : "#374151"}
                    >
                      {String(m).padStart(2, "0")}
                    </text>
                  </g>
                );
              })
            )}
            <circle cx={cx} cy={cy} r="2" fill="#111827" />
          </svg>

          {step === "minute" && (
            <div className="flex items-center justify-between mt-2 text-xs">
              <div className="flex gap-1">
                <button
                  type="button"
                  onClick={() => bumpMinute(-1)}
                  className="px-2 py-1 border border-gray-300 rounded hover:bg-gray-50 font-mono"
                >
                  −1m
                </button>
                <button
                  type="button"
                  onClick={() => bumpMinute(1)}
                  className="px-2 py-1 border border-gray-300 rounded hover:bg-gray-50 font-mono"
                >
                  +1m
                </button>
              </div>
              <button
                type="button"
                onClick={() => {
                  onChange(toHHMM(hour, minute));
                  setOpen(false);
                  setStep("hour");
                }}
                className="px-3 py-1 bg-gray-900 text-white rounded hover:bg-gray-700"
              >
                Done
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
