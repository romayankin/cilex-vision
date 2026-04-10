-- P3-V04: Adaptive transit-time learning
-- Adds transit_distributions JSONB column to topology_edges and creates
-- a materialized view for per-edge, per-class transit statistics derived
-- from confirmed MTMC associations.

-- Step 1: Add JSONB column to topology_edges for learned distributions.
-- Default is empty array — populated by the adaptive_transit.py blender.
ALTER TABLE topology_edges
    ADD COLUMN IF NOT EXISTS transit_distributions JSONB DEFAULT '[]'::jsonb;

-- Step 2: Materialized view computing per-edge, per-class percentile stats
-- from global_track_links joined with local_tracks.
-- For each global track with links to different cameras, computes the
-- directed transit time (b.start_time - a.start_time) in milliseconds.
CREATE MATERIALIZED VIEW IF NOT EXISTS transit_time_stats AS
WITH track_pairs AS (
    SELECT
        gt.object_class,
        a.camera_id AS from_camera,
        b.camera_id AS to_camera,
        EXTRACT(EPOCH FROM (b.start_time - a.start_time)) * 1000.0 AS transit_ms
    FROM global_track_links la
    JOIN global_track_links lb
        ON la.global_track_id = lb.global_track_id
        AND la.local_track_id != lb.local_track_id
    JOIN local_tracks a ON la.local_track_id = a.local_track_id
    JOIN local_tracks b ON lb.local_track_id = b.local_track_id
    JOIN global_tracks gt ON la.global_track_id = gt.global_track_id
    WHERE a.camera_id != b.camera_id
      AND b.start_time > a.start_time   -- directed: from A to B
)
SELECT
    from_camera,
    to_camera,
    object_class,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY transit_ms) AS p50_ms,
    percentile_cont(0.90) WITHIN GROUP (ORDER BY transit_ms) AS p90_ms,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY transit_ms) AS p99_ms,
    COUNT(*) AS sample_count
FROM track_pairs
WHERE transit_ms > 0 AND transit_ms < 3600000  -- sanity: 0 < t < 1 hour
GROUP BY from_camera, to_camera, object_class;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS idx_transit_stats_edge_class
    ON transit_time_stats (from_camera, to_camera, object_class);
