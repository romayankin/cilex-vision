"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getUserRole, isAdmin } from "@/lib/auth";

type ModelStatus = "active" | "recommended" | "available" | "gpu_only";

interface Model {
  name: string;
  accuracy: string;
  params: string;
  cpu_ms: number;
  gpu_ms: number;
  status: ModelStatus;
  note: string;
}

interface Capability {
  id: string;
  name: string;
  metric: string;
}

type Need = "required" | "optional" | "none";

interface UseCase {
  segment: string;
  name: string;
  score: number;
  needs: Record<string, Need>;
}

const CAPABILITIES: Capability[] = [
  { id: "detection", name: "Object Detection", metric: "COCO mAP@50-95" },
  { id: "person_reid", name: "Person Re-ID", metric: "Market1501 Rank-1" },
  { id: "face_detect", name: "Face Detection", metric: "WIDER FACE AP" },
  { id: "face_recog", name: "Face Recognition", metric: "LFW accuracy" },
  { id: "lpr", name: "License Plate Recognition", metric: "Character accuracy" },
  { id: "color", name: "Color / Attributes", metric: "Top-1 accuracy" },
  { id: "events", name: "Event Detection", metric: "Precision" },
  { id: "counting", name: "People Counting", metric: "Line-cross accuracy" },
  { id: "tailgating", name: "Tailgating Detection", metric: "Precision" },
  { id: "similarity", name: "Similarity Search", metric: "Recall@10" },
  { id: "vehicle_reid", name: "Vehicle Re-ID", metric: "VeRi-776 Rank-1" },
  { id: "action", name: "Action Recognition", metric: "Kinetics-400 Top-1" },
];

const CAP_SHORT: Record<string, string> = {
  detection: "Det",
  person_reid: "ReID",
  face_detect: "Face",
  face_recog: "FR",
  lpr: "LPR",
  color: "Color",
  events: "Evt",
  counting: "Count",
  tailgating: "Tail",
  similarity: "Sim",
  vehicle_reid: "Veh",
  action: "Act",
};

const MODELS: Record<string, Model[]> = {
  detection: [
    { name: "YOLOv8n", accuracy: "37.3% mAP", params: "3.2M", cpu_ms: 35, gpu_ms: 1.5, status: "active", note: "Current — false positive issues" },
    { name: "YOLOv8s", accuracy: "44.9% mAP", params: "11.2M", cpu_ms: 70, gpu_ms: 3, status: "recommended", note: "Best CPU option" },
    { name: "YOLOv8m", accuracy: "50.2% mAP", params: "25.9M", cpu_ms: 160, gpu_ms: 6, status: "available", note: "" },
    { name: "RT-DETR-l", accuracy: "53.1% mAP", params: "32M", cpu_ms: 500, gpu_ms: 9, status: "gpu_only", note: "Best accuracy, needs GPU" },
  ],
  person_reid: [
    { name: "None (zero-vector)", accuracy: "0%", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Current stub — Re-ID broken" },
    { name: "OSNet-x0.25", accuracy: "90.8% rank-1", params: "0.5M", cpu_ms: 5, gpu_ms: 1, status: "recommended", note: "Best CPU option" },
    { name: "FastReID ResNet50", accuracy: "95.4% rank-1", params: "25M", cpu_ms: 55, gpu_ms: 5, status: "available", note: "" },
    { name: "TransReID ViT-S", accuracy: "96.2% rank-1", params: "22M", cpu_ms: 140, gpu_ms: 8, status: "gpu_only", note: "Best accuracy" },
    { name: "CLIP-ReID", accuracy: "97.1% rank-1", params: "150M+", cpu_ms: 330, gpu_ms: 25, status: "gpu_only", note: "Max accuracy, heavy" },
  ],
  face_detect: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "RetinaFace-mobile", accuracy: "90.7% AP", params: "1.7M", cpu_ms: 12, gpu_ms: 1, status: "recommended", note: "Best CPU option" },
    { name: "SCRFD-34G", accuracy: "96.1% AP", params: "10M", cpu_ms: 55, gpu_ms: 5, status: "gpu_only", note: "Best accuracy" },
  ],
  face_recog: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "ArcFace-MobileFaceNet", accuracy: "99.50% LFW", params: "1M", cpu_ms: 4, gpu_ms: 1, status: "recommended", note: "Best CPU option" },
    { name: "AdaFace-IR101", accuracy: "99.82% LFW", params: "65M", cpu_ms: 140, gpu_ms: 10, status: "gpu_only", note: "Best accuracy" },
  ],
  lpr: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "YOLOv8s + PaddleOCR", accuracy: "92% char", params: "~15M", cpu_ms: 80, gpu_ms: 10, status: "recommended", note: "Best CPU option" },
    { name: "LPRNet + STN", accuracy: "95% char", params: "5M", cpu_ms: 70, gpu_ms: 4, status: "gpu_only", note: "Best accuracy" },
  ],
  color: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "Histogram (no ML)", accuracy: "~65% top-1", params: "0", cpu_ms: 1, gpu_ms: 0, status: "available", note: "No model needed" },
    { name: "MobileNetV3-small", accuracy: "82% top-1", params: "2.5M", cpu_ms: 5, gpu_ms: 1, status: "recommended", note: "Best CPU option" },
    { name: "ResNet50 multi-task", accuracy: "91% top-1", params: "25M", cpu_ms: 40, gpu_ms: 4, status: "gpu_only", note: "Best accuracy" },
  ],
  events: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Rules not wired" },
    { name: "Rules (speed+zone)", accuracy: "95% precision", params: "0", cpu_ms: 0.1, gpu_ms: 0, status: "recommended", note: "No model needed" },
  ],
  counting: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "YOLO + line-cross", accuracy: "~97%", params: "0", cpu_ms: 0.1, gpu_ms: 0, status: "recommended", note: "Uses existing detector" },
  ],
  tailgating: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "Counting + access rules", accuracy: "~95%", params: "0", cpu_ms: 0.1, gpu_ms: 0, status: "recommended", note: "Uses counting + access control" },
  ],
  similarity: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Blocked by zero-vector embedder" },
    { name: "OSNet + pgvector", accuracy: "88% recall@10", params: "0.5M", cpu_ms: 5, gpu_ms: 1, status: "recommended", note: "Reuses Re-ID embedder" },
    { name: "CLIP ViT-B/32", accuracy: "92% + text search", params: "150M", cpu_ms: 0, gpu_ms: 25, status: "gpu_only", note: "Enables text search" },
  ],
  vehicle_reid: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "OSNet (vehicle)", accuracy: "92.3% rank-1", params: "2.2M", cpu_ms: 10, gpu_ms: 1, status: "recommended", note: "Best CPU option" },
    { name: "TransReID (vehicle)", accuracy: "97.1% rank-1", params: "22M", cpu_ms: 140, gpu_ms: 8, status: "gpu_only", note: "Best accuracy" },
  ],
  action: [
    { name: "None", accuracy: "—", params: "0", cpu_ms: 0, gpu_ms: 0, status: "active", note: "Not implemented" },
    { name: "MoViNet-A0", accuracy: "72.0% top-1", params: "3.1M", cpu_ms: 70, gpu_ms: 10, status: "recommended", note: "Best CPU option" },
    { name: "SlowFast-R50", accuracy: "79.8% top-1", params: "33M", cpu_ms: 500, gpu_ms: 30, status: "gpu_only", note: "Best accuracy" },
  ],
};

const USE_CASES: UseCase[] = [
  { segment: "General", name: "Entryway Security", score: 4.15,
    needs: { detection: "required", person_reid: "optional", face_detect: "optional", face_recog: "optional", lpr: "none", color: "optional", events: "required", counting: "required", tailgating: "required", similarity: "optional", vehicle_reid: "none", action: "none" }},
  { segment: "General", name: "License Plate Recognition", score: 4.23,
    needs: { detection: "required", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "required", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "optional", action: "none" }},
  { segment: "General", name: "Incident Management", score: 4.10,
    needs: { detection: "required", person_reid: "required", face_detect: "optional", face_recog: "optional", lpr: "none", color: "required", events: "required", counting: "none", tailgating: "none", similarity: "required", vehicle_reid: "optional", action: "none" }},
  { segment: "General", name: "Blue Light Stations", score: 2.28,
    needs: { detection: "required", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "none", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "General", name: "Parking Lot Security", score: 4.35,
    needs: { detection: "required", person_reid: "optional", face_detect: "none", face_recog: "none", lpr: "required", color: "optional", events: "required", counting: "required", tailgating: "none", similarity: "optional", vehicle_reid: "required", action: "none" }},
  { segment: "General", name: "Mobile Surveillance", score: 3.52,
    needs: { detection: "required", person_reid: "optional", face_detect: "none", face_recog: "none", lpr: "optional", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "General", name: "Guard Efficiency", score: 4.43,
    needs: { detection: "required", person_reid: "optional", face_detect: "optional", face_recog: "optional", lpr: "none", color: "optional", events: "required", counting: "required", tailgating: "optional", similarity: "optional", vehicle_reid: "none", action: "none" }},
  { segment: "General", name: "Vehicle Access Control", score: 4.23,
    needs: { detection: "required", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "required", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "required", action: "none" }},
  { segment: "General", name: "Distribution Center Security", score: 4.15,
    needs: { detection: "required", person_reid: "optional", face_detect: "none", face_recog: "none", lpr: "required", color: "optional", events: "required", counting: "required", tailgating: "optional", similarity: "optional", vehicle_reid: "required", action: "none" }},
  { segment: "General", name: "Executive Protection", score: 3.10,
    needs: { detection: "required", person_reid: "required", face_detect: "required", face_recog: "required", lpr: "none", color: "optional", events: "required", counting: "none", tailgating: "optional", similarity: "required", vehicle_reid: "none", action: "none" }},
  { segment: "General", name: "Emergency Response", score: 3.98,
    needs: { detection: "required", person_reid: "optional", face_detect: "none", face_recog: "none", lpr: "none", color: "none", events: "required", counting: "required", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "optional" }},
  { segment: "General", name: "Roll Call", score: 3.28,
    needs: { detection: "required", person_reid: "none", face_detect: "optional", face_recog: "optional", lpr: "none", color: "none", events: "none", counting: "required", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "Schools", name: "Vape Detection", score: 3.00,
    needs: { detection: "none", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "none", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "Schools", name: "Lockdown", score: 3.68,
    needs: { detection: "required", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "none", color: "none", events: "required", counting: "required", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "optional" }},
  { segment: "Schools", name: "School Investigations", score: 3.55,
    needs: { detection: "required", person_reid: "required", face_detect: "required", face_recog: "required", lpr: "none", color: "required", events: "required", counting: "none", tailgating: "none", similarity: "required", vehicle_reid: "none", action: "none" }},
  { segment: "Schools", name: "Perimeter Security", score: 3.68,
    needs: { detection: "required", person_reid: "required", face_detect: "required", face_recog: "required", lpr: "none", color: "required", events: "required", counting: "required", tailgating: "none", similarity: "required", vehicle_reid: "none", action: "none" }},
  { segment: "Schools", name: "School Bus Security", score: 1.92,
    needs: { detection: "required", person_reid: "optional", face_detect: "none", face_recog: "none", lpr: "optional", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "Higher Ed", name: "Dorm Security", score: 3.55,
    needs: { detection: "required", person_reid: "optional", face_detect: "optional", face_recog: "optional", lpr: "none", color: "none", events: "required", counting: "required", tailgating: "required", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "Manufacturing", name: "Emergency Preparedness", score: 4.10,
    needs: { detection: "required", person_reid: "none", face_detect: "optional", face_recog: "optional", lpr: "none", color: "none", events: "none", counting: "required", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "Manufacturing", name: "Environment, Health & Safety", score: 3.60,
    needs: { detection: "required", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "none", color: "none", events: "required", counting: "required", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "optional" }},
  { segment: "Retail", name: "Loss Prevention", score: 4.23,
    needs: { detection: "required", person_reid: "required", face_detect: "required", face_recog: "required", lpr: "none", color: "required", events: "required", counting: "required", tailgating: "none", similarity: "required", vehicle_reid: "none", action: "optional" }},
  { segment: "Retail", name: "Fraud Prevention", score: 3.98,
    needs: { detection: "required", person_reid: "required", face_detect: "required", face_recog: "required", lpr: "none", color: "optional", events: "required", counting: "required", tailgating: "none", similarity: "required", vehicle_reid: "none", action: "none" }},
  { segment: "Retail", name: "Retail Employee Safety", score: 4.18,
    needs: { detection: "required", person_reid: "required", face_detect: "required", face_recog: "required", lpr: "none", color: "required", events: "required", counting: "none", tailgating: "none", similarity: "required", vehicle_reid: "none", action: "optional" }},
  { segment: "Retail", name: "Retail Analytics", score: 3.85,
    needs: { detection: "required", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "none", color: "none", events: "none", counting: "required", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "Law Enforcement", name: "Real-Time Crime Center", score: 3.15,
    needs: { detection: "required", person_reid: "required", face_detect: "required", face_recog: "required", lpr: "required", color: "required", events: "required", counting: "required", tailgating: "required", similarity: "required", vehicle_reid: "required", action: "required" }},
  { segment: "Law Enforcement", name: "ALPR", score: 4.23,
    needs: { detection: "required", person_reid: "none", face_detect: "none", face_recog: "none", lpr: "required", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "required", action: "none" }},
  { segment: "Law Enforcement", name: "Mobile Overwatch", score: 3.10,
    needs: { detection: "required", person_reid: "optional", face_detect: "none", face_recog: "none", lpr: "optional", color: "none", events: "required", counting: "none", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "Healthcare", name: "Pharmacy Security", score: 3.43,
    needs: { detection: "required", person_reid: "optional", face_detect: "none", face_recog: "none", lpr: "none", color: "none", events: "required", counting: "none", tailgating: "required", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "General", name: "Tailgating Detection", score: 0,
    needs: { detection: "required", person_reid: "required", face_detect: "optional", face_recog: "optional", lpr: "none", color: "none", events: "required", counting: "required", tailgating: "required", similarity: "none", vehicle_reid: "none", action: "none" }},
  { segment: "General", name: "Face in Crowd", score: 0,
    needs: { detection: "required", person_reid: "none", face_detect: "required", face_recog: "required", lpr: "none", color: "none", events: "none", counting: "required", tailgating: "none", similarity: "none", vehicle_reid: "none", action: "none" }},
];

type Hardware = "cpu" | "gpu";

const BUDGET_MS_PER_FRAME = 500; // 2 fps

// Pick the index of the first model matching a predicate, else 0 ("None").
function findIdx(cap: string, pred: (m: Model) => boolean): number {
  const list = MODELS[cap];
  const idx = list.findIndex(pred);
  return idx >= 0 ? idx : 0;
}

const PRESETS: Record<string, Record<string, number>> = {
  current: Object.fromEntries(
    CAPABILITIES.map((c) => [c.id, findIdx(c.id, (m) => m.status === "active")])
  ),
  recommended_cpu: Object.fromEntries(
    CAPABILITIES.map((c) => [c.id, findIdx(c.id, (m) => m.status === "recommended")])
  ),
  premium_gpu: Object.fromEntries(
    CAPABILITIES.map((c) => {
      const list = MODELS[c.id];
      // Pick gpu_only if available, else the highest-accuracy non-None model
      const gpuIdx = list.findIndex((m) => m.status === "gpu_only");
      if (gpuIdx >= 0) return [c.id, gpuIdx];
      const recIdx = list.findIndex((m) => m.status === "recommended");
      return [c.id, recIdx >= 0 ? recIdx : 0];
    })
  ),
  maximum: Object.fromEntries(
    CAPABILITIES.map((c) => {
      // Highest index model (heaviest = most accurate in this list order)
      const list = MODELS[c.id];
      return [c.id, list.length - 1];
    })
  ),
};

function isNone(m: Model): boolean {
  return m.name === "None" || m.name.startsWith("None ");
}

function modelLatency(m: Model, hw: Hardware): number {
  return hw === "cpu" ? m.cpu_ms : m.gpu_ms;
}

function encodeSelection(sel: Record<string, number>): string {
  return CAPABILITIES.map((c) => `${c.id}=${sel[c.id] ?? 0}`).join("&");
}

function decodeSelection(search: URLSearchParams): Record<string, number> | null {
  const preset = search.get("preset");
  if (preset && PRESETS[preset]) {
    return { ...PRESETS[preset] };
  }
  const out: Record<string, number> = {};
  let any = false;
  for (const c of CAPABILITIES) {
    const v = search.get(c.id);
    if (v !== null) {
      const idx = parseInt(v, 10);
      if (!isNaN(idx) && idx >= 0 && idx < MODELS[c.id].length) {
        out[c.id] = idx;
        any = true;
      }
    }
  }
  return any ? { ...PRESETS.current, ...out } : null;
}

function StatusDot({ status }: { status: ModelStatus }) {
  const color =
    status === "active"
      ? "bg-green-500"
      : status === "recommended"
        ? "bg-blue-500"
        : status === "gpu_only"
          ? "bg-orange-400"
          : "bg-gray-300";
  return <span className={`inline-block w-2 h-2 rounded-full ${color}`} />;
}

export default function PlannerPage() {
  const role = getUserRole();
  const [hardware, setHardware] = useState<Hardware>("cpu");
  const [selection, setSelection] = useState<Record<string, number>>(PRESETS.current);
  const [highlightedUseCase, setHighlightedUseCase] = useState<string | null>(null);
  const initializedRef = useRef(false);
  const matrixRef = useRef<HTMLDivElement | null>(null);

  // Init from URL
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;
    const params = new URLSearchParams(window.location.search);
    const hw = params.get("hw");
    if (hw === "cpu" || hw === "gpu") setHardware(hw);
    const decoded = decodeSelection(params);
    if (decoded) setSelection(decoded);
  }, []);

  // Sync URL when selection/hardware changes
  useEffect(() => {
    if (!initializedRef.current) return;
    const qs = `${encodeSelection(selection)}&hw=${hardware}`;
    const url = `${window.location.pathname}?${qs}`;
    window.history.replaceState(null, "", url);
  }, [selection, hardware]);

  const applyPreset = useCallback((key: keyof typeof PRESETS) => {
    setSelection({ ...PRESETS[key] });
  }, []);

  // Per-frame latency totals
  const { totalMs, perCap } = useMemo(() => {
    let total = 0;
    const rows: { cap: Capability; model: Model; ms: number }[] = [];
    for (const cap of CAPABILITIES) {
      const idx = selection[cap.id] ?? 0;
      const model = MODELS[cap.id][idx];
      const ms = modelLatency(model, hardware);
      total += ms;
      rows.push({ cap, model, ms });
    }
    return { totalMs: total, perCap: rows };
  }, [selection, hardware]);

  const maxCameras = totalMs > 0 ? Math.floor(BUDGET_MS_PER_FRAME / totalMs) : Infinity;
  const budgetPct = Math.min(100, (totalMs / BUDGET_MS_PER_FRAME) * 100);

  // Feasibility per use case
  const feasibility = useMemo(() => {
    return USE_CASES.map((uc) => {
      const missing: string[] = [];
      const haveOptional: string[] = [];
      const missingOptional: string[] = [];
      for (const cap of CAPABILITIES) {
        const need = uc.needs[cap.id];
        if (need === "none") continue;
        const idx = selection[cap.id] ?? 0;
        const model = MODELS[cap.id][idx];
        const active = !isNone(model);
        if (need === "required" && !active) missing.push(cap.name);
        if (need === "optional") {
          if (active) haveOptional.push(cap.name);
          else missingOptional.push(cap.name);
        }
      }
      let status: "feasible" | "partial" | "missing";
      if (missing.length === 0) status = "feasible";
      else if (missing.length <= 2) status = "partial";
      else status = "missing";
      return { uc, status, missing, haveOptional, missingOptional };
    });
  }, [selection]);

  const counts = useMemo(() => {
    const c = { feasible: 0, partial: 0, missing: 0 };
    for (const f of feasibility) c[f.status]++;
    return c;
  }, [feasibility]);

  const sortedFeasibility = useMemo(() => {
    const order: Record<string, number> = { feasible: 0, partial: 1, missing: 2 };
    return [...feasibility].sort((a, b) => {
      const d = order[a.status] - order[b.status];
      if (d !== 0) return d;
      return b.uc.score - a.uc.score;
    });
  }, [feasibility]);

  const highlightedCaps = useMemo(() => {
    if (!highlightedUseCase) return null;
    const uc = USE_CASES.find((u) => u.name === highlightedUseCase);
    return uc?.needs ?? null;
  }, [highlightedUseCase]);

  if (!isAdmin(role)) {
    return <p className="text-sm text-red-600">Admin access required.</p>;
  }

  const budgetColor =
    budgetPct < 60 ? "bg-green-500" : budgetPct < 90 ? "bg-amber-500" : "bg-red-500";

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Use Case Planner</h1>
          <div className="text-xs text-gray-500 mt-0.5">
            Select AI models per capability and see which of the {USE_CASES.length} surveillance
            use cases become feasible on this hardware.
          </div>
        </div>
        <div className="inline-flex rounded-md border border-gray-200 bg-white text-xs overflow-hidden">
          <button
            onClick={() => setHardware("cpu")}
            className={`px-3 py-1.5 ${hardware === "cpu" ? "bg-blue-600 text-white" : "text-gray-700 hover:bg-gray-50"}`}
          >
            CPU (i5-13500)
          </button>
          <button
            onClick={() => setHardware("gpu")}
            className={`px-3 py-1.5 border-l border-gray-200 ${hardware === "gpu" ? "bg-blue-600 text-white" : "text-gray-700 hover:bg-gray-50"}`}
          >
            + RTX A2000 GPU
          </button>
        </div>
      </div>

      {/* Presets */}
      <section className="bg-white border border-gray-200 rounded-lg p-4">
        <div className="flex items-center justify-between mb-2">
          <h2 className="font-medium text-sm">Quick Presets</h2>
          <span className="text-xs text-gray-400">URL-shareable configuration</span>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => applyPreset("current")}
            className="text-xs px-3 py-1.5 bg-gray-100 hover:bg-gray-200 rounded border border-gray-200"
          >
            Current System
          </button>
          <button
            onClick={() => applyPreset("recommended_cpu")}
            className="text-xs px-3 py-1.5 bg-blue-50 hover:bg-blue-100 text-blue-800 rounded border border-blue-200"
          >
            Recommended CPU
          </button>
          <button
            onClick={() => applyPreset("premium_gpu")}
            className="text-xs px-3 py-1.5 bg-indigo-50 hover:bg-indigo-100 text-indigo-800 rounded border border-indigo-200"
          >
            Premium GPU
          </button>
          <button
            onClick={() => applyPreset("maximum")}
            className="text-xs px-3 py-1.5 bg-purple-50 hover:bg-purple-100 text-purple-800 rounded border border-purple-200"
          >
            Maximum Accuracy
          </button>
        </div>
      </section>

      {/* Model selection */}
      <section className="bg-white border border-gray-200 rounded-lg p-4">
        <h2 className="font-medium text-sm mb-3">Model Selection</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2">
          {perCap.map(({ cap, model, ms }) => {
            const needed = highlightedCaps?.[cap.id];
            const ringClass =
              needed === "required"
                ? "ring-2 ring-blue-400"
                : needed === "optional"
                  ? "ring-1 ring-blue-200"
                  : "";
            const active = !isNone(model);
            return (
              <div
                key={cap.id}
                className={`flex items-center gap-2 py-1.5 px-2 rounded ${ringClass}`}
              >
                <div className="w-40 text-xs text-gray-700 shrink-0">
                  {cap.name}
                  {needed === "required" && (
                    <span className="ml-1 text-[10px] text-blue-600">★</span>
                  )}
                </div>
                <select
                  value={selection[cap.id] ?? 0}
                  onChange={(e) =>
                    setSelection((s) => ({ ...s, [cap.id]: parseInt(e.target.value, 10) }))
                  }
                  className="flex-1 text-xs border border-gray-200 rounded px-2 py-1 bg-white font-mono"
                >
                  {MODELS[cap.id].map((m, i) => {
                    const disabled = hardware === "cpu" && m.status === "gpu_only";
                    return (
                      <option key={i} value={i} disabled={disabled}>
                        {m.name} — {m.accuracy}
                        {m.cpu_ms > 0 || m.gpu_ms > 0
                          ? ` (${hardware === "cpu" ? m.cpu_ms : m.gpu_ms}ms ${hardware})`
                          : ""}
                        {disabled ? " [needs GPU]" : ""}
                      </option>
                    );
                  })}
                </select>
                <div className="w-10 shrink-0 flex justify-center">
                  <StatusDot status={model.status} />
                </div>
                <div
                  className={`w-16 shrink-0 text-right text-xs font-mono ${active ? "text-gray-700" : "text-gray-300"}`}
                >
                  {ms > 0 ? `${ms.toFixed(1)}ms` : "—"}
                </div>
              </div>
            );
          })}
        </div>

        <div className="mt-4 pt-4 border-t border-gray-100 space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-600">
              Total per frame: <span className="font-mono font-semibold">{totalMs.toFixed(1)}ms</span>{" "}
              {hardware.toUpperCase()}
            </span>
            <span className="text-gray-600">
              Max cameras @ 2fps:{" "}
              <span className={`font-mono font-semibold ${maxCameras < 1 ? "text-red-600" : "text-gray-900"}`}>
                {maxCameras === Infinity ? "∞" : maxCameras}
              </span>
            </span>
          </div>
          <div className="h-2 bg-gray-100 rounded overflow-hidden">
            <div
              className={`h-full ${budgetColor} transition-all duration-300`}
              style={{ width: `${budgetPct}%` }}
            />
          </div>
          <div className="text-[11px] text-gray-500">
            {budgetPct.toFixed(0)}% of {BUDGET_MS_PER_FRAME}ms single-camera 2fps budget
          </div>
        </div>

        <div className="mt-3 flex items-center gap-4 text-[11px] text-gray-500">
          <span className="inline-flex items-center gap-1.5">
            <StatusDot status="active" /> Active
          </span>
          <span className="inline-flex items-center gap-1.5">
            <StatusDot status="recommended" /> Recommended
          </span>
          <span className="inline-flex items-center gap-1.5">
            <StatusDot status="available" /> Available
          </span>
          <span className="inline-flex items-center gap-1.5">
            <StatusDot status="gpu_only" /> GPU-only
          </span>
        </div>
      </section>

      {/* Feasibility */}
      <section className="bg-white border border-gray-200 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-medium text-sm">Use Case Feasibility</h2>
          <div className="flex items-center gap-3 text-xs">
            <span className="text-green-700">
              ✓ Feasible <span className="font-mono">{counts.feasible}</span>
            </span>
            <span className="text-amber-700">
              ⚠ Partial <span className="font-mono">{counts.partial}</span>
            </span>
            <span className="text-red-700">
              ✗ Missing <span className="font-mono">{counts.missing}</span>
            </span>
          </div>
        </div>
        <div className="space-y-1 max-h-96 overflow-y-auto">
          {sortedFeasibility.map(({ uc, status, missing, missingOptional }) => {
            const icon = status === "feasible" ? "✓" : status === "partial" ? "⚠" : "✗";
            const iconColor =
              status === "feasible"
                ? "text-green-600"
                : status === "partial"
                  ? "text-amber-600"
                  : "text-red-600";
            const isHl = highlightedUseCase === uc.name;
            return (
              <button
                key={`${uc.segment}-${uc.name}`}
                onClick={() => {
                  setHighlightedUseCase(isHl ? null : uc.name);
                  if (!isHl) {
                    setTimeout(() => {
                      matrixRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
                    }, 50);
                  }
                }}
                className={`w-full flex items-start gap-2 text-left px-2 py-1.5 rounded text-xs hover:bg-gray-50 ${isHl ? "bg-blue-50 ring-1 ring-blue-300" : ""}`}
              >
                <span className={`font-mono w-5 shrink-0 ${iconColor}`}>{icon}</span>
                <span className="w-44 shrink-0 font-medium text-gray-800 truncate">
                  {uc.name}
                </span>
                <span className="w-28 shrink-0 text-gray-400 truncate">{uc.segment}</span>
                <span className="w-12 shrink-0 text-right font-mono text-gray-500">
                  {uc.score > 0 ? uc.score.toFixed(2) : "—"}
                </span>
                <span className="flex-1 text-gray-500 truncate">
                  {status === "feasible"
                    ? missingOptional.length === 0
                      ? "All required + optional capabilities active"
                      : `All required active; optional missing: ${missingOptional.join(", ")}`
                    : `Missing: ${missing.join(", ")}`}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      {/* Capability matrix */}
      <section ref={matrixRef} className="bg-white border border-gray-200 rounded-lg p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="font-medium text-sm">Capability Matrix</h2>
          <span className="text-[11px] text-gray-400">
            Click a use case above to highlight its requirements
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="text-gray-500 border-b border-gray-200">
                <th className="text-left py-1.5 pr-2 font-medium">Use case</th>
                <th className="text-left py-1.5 pr-2 font-medium">Segment</th>
                {CAPABILITIES.map((c) => {
                  const hl = highlightedCaps?.[c.id];
                  const hlClass =
                    hl === "required"
                      ? "bg-blue-100 text-blue-800"
                      : hl === "optional"
                        ? "bg-blue-50 text-blue-700"
                        : "";
                  return (
                    <th
                      key={c.id}
                      className={`text-center py-1.5 px-1 font-medium ${hlClass}`}
                      title={c.name}
                    >
                      {CAP_SHORT[c.id]}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {USE_CASES.map((uc) => {
                const isHl = highlightedUseCase === uc.name;
                return (
                  <tr
                    key={`${uc.segment}-${uc.name}`}
                    className={`border-b border-gray-50 hover:bg-gray-50 cursor-pointer ${isHl ? "bg-blue-50" : ""}`}
                    onClick={() => setHighlightedUseCase(isHl ? null : uc.name)}
                  >
                    <td className="py-1 pr-2 text-gray-800 truncate max-w-[200px]">{uc.name}</td>
                    <td className="py-1 pr-2 text-gray-400 truncate max-w-[120px]">{uc.segment}</td>
                    {CAPABILITIES.map((c) => {
                      const need = uc.needs[c.id];
                      const idx = selection[c.id] ?? 0;
                      const model = MODELS[c.id][idx];
                      const active = !isNone(model);
                      let content = "—";
                      let cls = "text-gray-200";
                      if (need === "required") {
                        content = active ? "●" : "✗";
                        cls = active ? "text-green-600" : "text-red-500";
                      } else if (need === "optional") {
                        content = active ? "◉" : "○";
                        cls = active ? "text-blue-500" : "text-gray-400";
                      }
                      return (
                        <td key={c.id} className={`text-center py-1 px-1 font-mono ${cls}`}>
                          {content}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="mt-3 flex items-center gap-4 text-[11px] text-gray-500">
          <span>
            <span className="text-green-600 font-mono">●</span> required active
          </span>
          <span>
            <span className="text-red-500 font-mono">✗</span> required missing
          </span>
          <span>
            <span className="text-blue-500 font-mono">◉</span> optional active
          </span>
          <span>
            <span className="text-gray-400 font-mono">○</span> optional missing
          </span>
        </div>
      </section>
    </div>
  );
}
