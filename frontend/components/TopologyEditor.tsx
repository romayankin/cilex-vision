"use client";

import { useState, useCallback } from "react";
import type { TopologyGraph, CameraNode, TransitionEdge } from "@/lib/api-client";

interface TopologyEditorProps {
  topology: TopologyGraph;
  onAddEdge: (cameraA: string, cameraB: string) => void;
  onRemoveEdge: (cameraA: string, cameraB: string) => void;
  onSelectCamera: (camera: CameraNode) => void;
}

const NODE_RADIUS = 28;
const SVG_WIDTH = 800;
const SVG_HEIGHT = 500;

/** Distribute cameras in a circle layout. */
function layoutNodes(cameras: CameraNode[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const cx = SVG_WIDTH / 2;
  const cy = SVG_HEIGHT / 2;
  const radius = Math.min(SVG_WIDTH, SVG_HEIGHT) * 0.35;

  cameras.forEach((cam, i) => {
    const angle = (2 * Math.PI * i) / cameras.length - Math.PI / 2;
    positions.set(cam.camera_id, {
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    });
  });
  return positions;
}

function statusColor(status: string): string {
  return status === "online" ? "#22c55e" : "#ef4444";
}

export default function TopologyEditor({
  topology,
  onAddEdge,
  onRemoveEdge,
  onSelectCamera,
}: TopologyEditorProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const [linkSource, setLinkSource] = useState<string | null>(null);

  const positions = layoutNodes(topology.cameras);

  const handleNodeClick = useCallback(
    (cam: CameraNode) => {
      if (linkSource) {
        if (linkSource !== cam.camera_id) {
          onAddEdge(linkSource, cam.camera_id);
        }
        setLinkSource(null);
        return;
      }
      setSelected(cam.camera_id);
      onSelectCamera(cam);
    },
    [linkSource, onAddEdge, onSelectCamera]
  );

  const startLink = useCallback((cameraId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setLinkSource(cameraId);
  }, []);

  const handleEdgeClick = useCallback(
    (edge: TransitionEdge) => {
      onRemoveEdge(edge.camera_a_id, edge.camera_b_id);
    },
    [onRemoveEdge]
  );

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3 text-xs text-gray-500">
        <span>Click node to select</span>
        <span>|</span>
        <span>Right-click node to start edge, then click target</span>
        <span>|</span>
        <span>Click edge label to remove</span>
        {linkSource && (
          <span className="text-blue-600 font-medium">
            Linking from {linkSource}... click target node
          </span>
        )}
      </div>

      <svg
        width={SVG_WIDTH}
        height={SVG_HEIGHT}
        className="border border-gray-200 rounded-lg bg-white"
        onClick={() => { setLinkSource(null); setSelected(null); }}
      >
        {/* Edges */}
        {topology.edges.map((edge) => {
          const a = positions.get(edge.camera_a_id);
          const b = positions.get(edge.camera_b_id);
          if (!a || !b) return null;
          const mx = (a.x + b.x) / 2;
          const my = (a.y + b.y) / 2;
          return (
            <g key={`${edge.camera_a_id}-${edge.camera_b_id}`}>
              <line
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke={edge.enabled ? "#6b7280" : "#d1d5db"}
                strokeWidth={2}
                strokeDasharray={edge.enabled ? "none" : "6,4"}
              />
              <g
                onClick={(e) => { e.stopPropagation(); handleEdgeClick(edge); }}
                className="cursor-pointer"
              >
                <rect
                  x={mx - 24}
                  y={my - 10}
                  width={48}
                  height={20}
                  rx={4}
                  fill="white"
                  stroke="#d1d5db"
                  strokeWidth={1}
                />
                <text
                  x={mx}
                  y={my + 4}
                  textAnchor="middle"
                  fontSize={10}
                  fill="#374151"
                >
                  {edge.transition_time_s}s
                </text>
              </g>
            </g>
          );
        })}

        {/* Camera nodes */}
        {topology.cameras.map((cam) => {
          const pos = positions.get(cam.camera_id);
          if (!pos) return null;
          const isSelected = selected === cam.camera_id;
          const isLinkSource = linkSource === cam.camera_id;
          return (
            <g
              key={cam.camera_id}
              onClick={(e) => { e.stopPropagation(); handleNodeClick(cam); }}
              onContextMenu={(e) => { e.preventDefault(); startLink(cam.camera_id, e); }}
              className="cursor-pointer"
            >
              <circle
                cx={pos.x}
                cy={pos.y}
                r={NODE_RADIUS}
                fill={isLinkSource ? "#dbeafe" : "white"}
                stroke={isSelected ? "#2563eb" : "#9ca3af"}
                strokeWidth={isSelected ? 3 : 1.5}
              />
              {/* Status indicator */}
              <circle
                cx={pos.x + NODE_RADIUS * 0.6}
                cy={pos.y - NODE_RADIUS * 0.6}
                r={5}
                fill={statusColor(cam.status)}
                stroke="white"
                strokeWidth={1.5}
              />
              {/* Camera ID label */}
              <text
                x={pos.x}
                y={pos.y - 3}
                textAnchor="middle"
                fontSize={9}
                fontWeight={500}
                fill="#111827"
              >
                {cam.camera_id.length > 10
                  ? cam.camera_id.slice(0, 10) + "..."
                  : cam.camera_id}
              </text>
              {/* Zone label */}
              {cam.zone_id && (
                <text
                  x={pos.x}
                  y={pos.y + 10}
                  textAnchor="middle"
                  fontSize={8}
                  fill="#6b7280"
                >
                  {cam.zone_id}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
