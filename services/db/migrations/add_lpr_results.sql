CREATE TABLE IF NOT EXISTS lpr_results (
    result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    local_track_id UUID NOT NULL REFERENCES local_tracks(local_track_id),
    camera_id TEXT NOT NULL,
    plate_text TEXT NOT NULL,
    plate_confidence FLOAT NOT NULL,
    country_format TEXT,
    plate_bbox_x FLOAT NOT NULL,
    plate_bbox_y FLOAT NOT NULL,
    plate_bbox_w FLOAT NOT NULL,
    plate_bbox_h FLOAT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_version TEXT
);

CREATE INDEX IF NOT EXISTS idx_lpr_results_plate_text ON lpr_results (plate_text);
CREATE INDEX IF NOT EXISTS idx_lpr_results_camera_time ON lpr_results (camera_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_lpr_results_track ON lpr_results (local_track_id);
