/**
 * Schema of events.metadata_jsonb — v1.
 * See Phase 1 refactor for the design rationale.
 */

export interface EventMetadata {
  version: number;
  motion_interval: {
    started_at: string;     // ISO
    ended_at: string;
    duration_s: number;
    peak_at: string;
  };
  objects: {
    [objectClass: string]: {
      count: number;
      total_frames_seen: number;
      attributes: {
        upper_colors?: string[];
        lower_colors?: string[];
        colors?: string[];
      };
      track_ids: string[];
    };
  };
  zones_triggered: string[];
  model_version: string;
  processing: {
    frames_analyzed: number;
    processing_ms: number;
    attributes_enabled: boolean;
  };
}
