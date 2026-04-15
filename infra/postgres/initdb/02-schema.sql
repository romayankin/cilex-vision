-- Application schema for Cilex Vision.
-- Mirrors services/db/models.py.  Runs once on first container start
-- (when the data volume is empty).  All DDL is idempotent.

-- ===================================================================
--  Sites & cameras
-- ===================================================================

CREATE TABLE IF NOT EXISTS sites (
    site_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR(255) NOT NULL,
    address       TEXT,
    timezone      VARCHAR(50) NOT NULL DEFAULT 'UTC',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS cameras (
    camera_id            TEXT PRIMARY KEY,
    site_id              UUID NOT NULL REFERENCES sites(site_id),
    name                 VARCHAR(255) NOT NULL,
    rtsp_uri             TEXT,
    location_description TEXT,
    latitude             DOUBLE PRECISION,
    longitude            DOUBLE PRECISION,
    status               VARCHAR(20) NOT NULL DEFAULT 'offline',
    config_json          JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ,
    CONSTRAINT ck_cameras_status CHECK (
        status IN ('online', 'offline', 'maintenance', 'error')
    )
);

CREATE TABLE IF NOT EXISTS topology_edges (
    edge_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_a_id      TEXT NOT NULL REFERENCES cameras(camera_id),
    camera_b_id      TEXT NOT NULL REFERENCES cameras(camera_id),
    transition_time_s DOUBLE PRECISION NOT NULL,
    confidence       DOUBLE PRECISION NOT NULL,
    enabled          BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===================================================================
--  Detections hypertable
-- ===================================================================

CREATE TABLE IF NOT EXISTS detections (
    time            TIMESTAMPTZ NOT NULL,
    camera_id       TEXT NOT NULL,
    frame_seq       BIGINT NOT NULL,
    object_class    VARCHAR(20) NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    bbox_x          DOUBLE PRECISION NOT NULL,
    bbox_y          DOUBLE PRECISION NOT NULL,
    bbox_w          DOUBLE PRECISION NOT NULL,
    bbox_h          DOUBLE PRECISION NOT NULL,
    local_track_id  UUID,
    model_version   VARCHAR(50) NOT NULL,
    CONSTRAINT ck_detections_class CHECK (
        object_class IN ('person','car','truck','bus','bicycle','motorcycle','animal')
    )
);

SELECT create_hypertable('detections', 'time',
       chunk_time_interval => INTERVAL '1 hour',
       if_not_exists => TRUE);

-- ===================================================================
--  Track observations hypertable
-- ===================================================================

CREATE TABLE IF NOT EXISTS track_observations (
    time            TIMESTAMPTZ NOT NULL,
    camera_id       TEXT NOT NULL,
    local_track_id  UUID NOT NULL,
    centroid_x      DOUBLE PRECISION NOT NULL,
    centroid_y      DOUBLE PRECISION NOT NULL,
    bbox_area       DOUBLE PRECISION NOT NULL,
    embedding_ref   TEXT
);

SELECT create_hypertable('track_observations', 'time',
       chunk_time_interval => INTERVAL '1 hour',
       if_not_exists => TRUE);

-- ===================================================================
--  Tracks & attributes
-- ===================================================================

CREATE TABLE IF NOT EXISTS local_tracks (
    local_track_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id       TEXT NOT NULL REFERENCES cameras(camera_id),
    object_class    VARCHAR(20) NOT NULL,
    state           VARCHAR(20) NOT NULL,
    mean_confidence DOUBLE PRECISION,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ,
    tracker_version VARCHAR(50),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_local_tracks_class CHECK (
        object_class IN ('person','car','truck','bus','bicycle','motorcycle','animal')
    ),
    CONSTRAINT ck_local_tracks_state CHECK (
        state IN ('new','active','lost','terminated')
    )
);

CREATE TABLE IF NOT EXISTS global_tracks (
    global_track_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    object_class    VARCHAR(20) NOT NULL,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_global_tracks_class CHECK (
        object_class IN ('person','car','truck','bus','bicycle','motorcycle','animal')
    )
);

CREATE TABLE IF NOT EXISTS global_track_links (
    link_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    global_track_id UUID NOT NULL REFERENCES global_tracks(global_track_id),
    local_track_id  UUID NOT NULL REFERENCES local_tracks(local_track_id),
    camera_id       TEXT NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    linked_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS track_attributes (
    attribute_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    local_track_id  UUID NOT NULL REFERENCES local_tracks(local_track_id),
    attribute_type  VARCHAR(30) NOT NULL,
    color_value     VARCHAR(20) NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    model_version   VARCHAR(50),
    observed_at     TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_track_attrs_type CHECK (
        attribute_type IN ('vehicle_color','person_upper_color','person_lower_color')
    ),
    CONSTRAINT ck_track_attrs_color CHECK (
        color_value IN ('red','blue','white','black','silver','green','yellow','brown','orange','unknown')
    )
);

-- ===================================================================
--  Events
-- ===================================================================

CREATE TABLE IF NOT EXISTS events (
    event_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type       VARCHAR(30) NOT NULL,
    track_id         UUID REFERENCES local_tracks(local_track_id),
    camera_id        TEXT NOT NULL REFERENCES cameras(camera_id),
    start_time       TIMESTAMPTZ NOT NULL,
    end_time         TIMESTAMPTZ,
    duration_ms      BIGINT,
    clip_uri         TEXT,
    state            VARCHAR(20) NOT NULL,
    metadata_jsonb   JSONB,
    source_capture_ts TIMESTAMPTZ,
    edge_receive_ts  TIMESTAMPTZ,
    core_ingest_ts   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ,
    CONSTRAINT ck_events_type CHECK (
        event_type IN ('entered_scene','exited_scene','stopped','loitering','motion_started','motion_ended')
    ),
    CONSTRAINT ck_events_state CHECK (
        state IN ('new','active','stopped','exited','closed')
    )
);

-- ===================================================================
--  Auth
-- ===================================================================

CREATE TABLE IF NOT EXISTS users (
    user_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      VARCHAR(150) UNIQUE NOT NULL,
    email         VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          VARCHAR(50) NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ
);

-- No FK on user_id: the built-in admin JWT has no row in users, and audit
-- trails should survive user deletion.
CREATE TABLE IF NOT EXISTS audit_logs (
    log_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID,
    action        TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT,
    details_jsonb JSONB,
    ip_address    VARCHAR(45),
    hostname      VARCHAR(255),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created  ON audit_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action   ON audit_logs (action);
CREATE INDEX IF NOT EXISTS idx_audit_logs_resource ON audit_logs (resource_type);

-- Access log: one row per read-only API request. High-volume, auto-pruned
-- after 90 days via TimescaleDB retention policy. No PK because hypertable
-- rows cannot have a PK without the partition column.
CREATE TABLE IF NOT EXISTS access_log (
    id            BIGSERIAL,
    user_id       UUID,
    username      VARCHAR(100),
    method        VARCHAR(10) NOT NULL,
    path          TEXT NOT NULL,
    query_string  TEXT,
    status_code   SMALLINT,
    latency_ms    REAL,
    ip_address    VARCHAR(45),
    hostname      VARCHAR(255),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
SELECT create_hypertable('access_log', 'created_at', if_not_exists => TRUE);
SELECT add_retention_policy('access_log', INTERVAL '90 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_access_log_created ON access_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_access_log_user    ON access_log (username, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_access_log_path    ON access_log (path, created_at DESC);

-- Seed default site
INSERT INTO sites (site_id, name, timezone)
VALUES ('00000000-0000-0000-0000-000000000001', 'Default Site', 'UTC')
ON CONFLICT (site_id) DO NOTHING;
