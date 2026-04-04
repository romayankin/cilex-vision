"""Initial schema: all tables, hypertables, retention, compression, indexes.

Revision ID: 0001
Revises: None
Create Date: 2026-04-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # A. Relational tables (in FK dependency order)
    # ------------------------------------------------------------------

    # 1. sites
    op.create_table(
        "sites",
        sa.Column("site_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("address", sa.Text, nullable=True),
        sa.Column("timezone", sa.String(50), server_default="UTC", nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TSTZ, nullable=True),
    )

    # 2. users
    op.create_table(
        "users",
        sa.Column("user_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("username", sa.String(150), unique=True, nullable=False),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("is_active", sa.Boolean, server_default="true", nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TSTZ, nullable=True),
    )

    # 3. cameras (FK -> sites)
    op.create_table(
        "cameras",
        sa.Column("camera_id", sa.Text, primary_key=True),
        sa.Column("site_id", UUID(as_uuid=True), sa.ForeignKey("sites.site_id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("rtsp_uri", sa.Text, nullable=True),
        sa.Column("location_description", sa.Text, nullable=True),
        sa.Column("latitude", sa.Float, nullable=True),
        sa.Column("longitude", sa.Float, nullable=True),
        sa.Column("status", sa.String(20), server_default="offline", nullable=False),
        sa.Column("config_json", JSONB, nullable=True),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TSTZ, nullable=True),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'maintenance', 'error')",
            name="ck_cameras_status",
        ),
    )

    # 4. topology_edges (FK -> cameras x2)
    op.create_table(
        "topology_edges",
        sa.Column("edge_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("camera_a_id", sa.Text, sa.ForeignKey("cameras.camera_id"), nullable=False),
        sa.Column("camera_b_id", sa.Text, sa.ForeignKey("cameras.camera_id"), nullable=False),
        sa.Column("transition_time_s", sa.Float, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("enabled", sa.Boolean, server_default="true", nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
    )

    # 5. local_tracks (FK -> cameras)
    op.create_table(
        "local_tracks",
        sa.Column("local_track_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("camera_id", sa.Text, sa.ForeignKey("cameras.camera_id"), nullable=False),
        sa.Column("object_class", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("mean_confidence", sa.Float, nullable=True),
        sa.Column("start_time", TSTZ, nullable=False),
        sa.Column("end_time", TSTZ, nullable=True),
        sa.Column("tracker_version", sa.String(50), nullable=True),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "object_class IN ('person', 'car', 'truck', 'bus', 'bicycle', 'motorcycle', 'animal')",
            name="ck_local_tracks_class",
        ),
        sa.CheckConstraint(
            "state IN ('new', 'active', 'lost', 'terminated')",
            name="ck_local_tracks_state",
        ),
    )

    # 6. global_tracks
    op.create_table(
        "global_tracks",
        sa.Column("global_track_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("object_class", sa.String(20), nullable=False),
        sa.Column("first_seen", TSTZ, nullable=False),
        sa.Column("last_seen", TSTZ, nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "object_class IN ('person', 'car', 'truck', 'bus', 'bicycle', 'motorcycle', 'animal')",
            name="ck_global_tracks_class",
        ),
    )

    # 7. global_track_links (FK -> global_tracks, local_tracks)
    op.create_table(
        "global_track_links",
        sa.Column("link_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("global_track_id", UUID(as_uuid=True), sa.ForeignKey("global_tracks.global_track_id"), nullable=False),
        sa.Column("local_track_id", UUID(as_uuid=True), sa.ForeignKey("local_tracks.local_track_id"), nullable=False),
        sa.Column("camera_id", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("linked_at", TSTZ, nullable=False),
    )

    # 8. track_attributes (FK -> local_tracks)
    op.create_table(
        "track_attributes",
        sa.Column("attribute_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("local_track_id", UUID(as_uuid=True), sa.ForeignKey("local_tracks.local_track_id"), nullable=False),
        sa.Column("attribute_type", sa.String(30), nullable=False),
        sa.Column("color_value", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("model_version", sa.String(50), nullable=True),
        sa.Column("observed_at", TSTZ, nullable=False),
        sa.CheckConstraint(
            "attribute_type IN ('vehicle_color', 'person_upper_color', 'person_lower_color')",
            name="ck_track_attrs_type",
        ),
        sa.CheckConstraint(
            "color_value IN ('red', 'blue', 'white', 'black', 'silver', 'green', 'yellow', 'brown', 'orange', 'unknown')",
            name="ck_track_attrs_color",
        ),
    )

    # 9. events (FK -> local_tracks, cameras)
    op.create_table(
        "events",
        sa.Column("event_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("track_id", UUID(as_uuid=True), sa.ForeignKey("local_tracks.local_track_id"), nullable=True),
        sa.Column("camera_id", sa.Text, sa.ForeignKey("cameras.camera_id"), nullable=False),
        sa.Column("start_time", TSTZ, nullable=False),
        sa.Column("end_time", TSTZ, nullable=True),
        sa.Column("duration_ms", sa.BigInteger, nullable=True),
        sa.Column("clip_uri", sa.Text, nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("metadata_jsonb", JSONB, nullable=True),
        sa.Column("source_capture_ts", TSTZ, nullable=True),
        sa.Column("edge_receive_ts", TSTZ, nullable=True),
        sa.Column("core_ingest_ts", TSTZ, nullable=True),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TSTZ, nullable=True),
        sa.CheckConstraint(
            "event_type IN ('entered_scene', 'exited_scene', 'stopped', 'loitering', 'motion_started', 'motion_ended')",
            name="ck_events_type",
        ),
        sa.CheckConstraint(
            "state IN ('new', 'active', 'stopped', 'exited', 'closed')",
            name="ck_events_state",
        ),
    )

    # 10. audit_logs (FK -> users)
    op.create_table(
        "audit_logs",
        sa.Column("log_id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("resource_type", sa.Text, nullable=False),
        sa.Column("resource_id", sa.Text, nullable=True),
        sa.Column("details_jsonb", JSONB, nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
    )

    # ------------------------------------------------------------------
    # B. Hypertable base tables
    # ------------------------------------------------------------------

    # 11. detections
    op.create_table(
        "detections",
        sa.Column("time", TSTZ, nullable=False),
        sa.Column("camera_id", sa.Text, nullable=False),
        sa.Column("frame_seq", sa.BigInteger, nullable=False),
        sa.Column("object_class", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("bbox_x", sa.Float, nullable=False),
        sa.Column("bbox_y", sa.Float, nullable=False),
        sa.Column("bbox_w", sa.Float, nullable=False),
        sa.Column("bbox_h", sa.Float, nullable=False),
        sa.Column("local_track_id", UUID(as_uuid=True), nullable=True),
        sa.Column("model_version", sa.String(50), nullable=False),
        sa.CheckConstraint(
            "object_class IN ('person', 'car', 'truck', 'bus', 'bicycle', 'motorcycle', 'animal')",
            name="ck_detections_class",
        ),
    )

    # 12. track_observations
    op.create_table(
        "track_observations",
        sa.Column("time", TSTZ, nullable=False),
        sa.Column("camera_id", sa.Text, nullable=False),
        sa.Column("local_track_id", UUID(as_uuid=True), nullable=False),
        sa.Column("centroid_x", sa.Float, nullable=False),
        sa.Column("centroid_y", sa.Float, nullable=False),
        sa.Column("bbox_area", sa.Float, nullable=False),
        sa.Column("embedding_ref", sa.Text, nullable=True),
    )

    # ------------------------------------------------------------------
    # C. Convert to TimescaleDB hypertables
    # ------------------------------------------------------------------

    op.execute(
        "SELECT create_hypertable('detections', 'time', "
        "chunk_time_interval => INTERVAL '1 hour', "
        "if_not_exists => TRUE)"
    )
    op.execute(
        "SELECT create_hypertable('track_observations', 'time', "
        "chunk_time_interval => INTERVAL '1 hour', "
        "if_not_exists => TRUE)"
    )

    # ------------------------------------------------------------------
    # D. Retention policies (30 days)
    # ------------------------------------------------------------------

    op.execute("SELECT add_retention_policy('detections', INTERVAL '30 days', if_not_exists => TRUE)")
    op.execute("SELECT add_retention_policy('track_observations', INTERVAL '30 days', if_not_exists => TRUE)")

    # ------------------------------------------------------------------
    # E. Compression policies (compress after 2 days)
    # ------------------------------------------------------------------

    op.execute(
        "ALTER TABLE detections SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'camera_id', "
        "timescaledb.compress_orderby = 'time DESC')"
    )
    op.execute("SELECT add_compression_policy('detections', INTERVAL '2 days', if_not_exists => TRUE)")

    op.execute(
        "ALTER TABLE track_observations SET ("
        "timescaledb.compress, "
        "timescaledb.compress_segmentby = 'camera_id', "
        "timescaledb.compress_orderby = 'time DESC')"
    )
    op.execute("SELECT add_compression_policy('track_observations', INTERVAL '2 days', if_not_exists => TRUE)")

    # ------------------------------------------------------------------
    # F. Indexes
    # ------------------------------------------------------------------

    # Hypertable indexes
    op.create_index("ix_detections_camera_time", "detections", ["camera_id", sa.text("time DESC")])
    op.create_index("ix_detections_class_time", "detections", ["object_class", sa.text("time DESC")])
    op.create_index("ix_detections_track", "detections", ["local_track_id", sa.text("time DESC")])
    op.create_index("ix_track_obs_camera_time", "track_observations", ["camera_id", sa.text("time DESC")])
    op.create_index("ix_track_obs_track", "track_observations", ["local_track_id", sa.text("time DESC")])

    # Relational indexes
    op.create_index("ix_cameras_site", "cameras", ["site_id"])
    op.create_index("ix_local_tracks_camera_state", "local_tracks", ["camera_id", "state"])
    op.create_index("ix_local_tracks_time_range", "local_tracks", ["start_time", "end_time"])
    op.create_index("ix_global_track_links_global", "global_track_links", ["global_track_id"])
    op.create_index("ix_global_track_links_local", "global_track_links", ["local_track_id"])
    op.create_index("ix_events_camera_time", "events", ["camera_id", sa.text("start_time DESC")])
    op.create_index("ix_events_type_state", "events", ["event_type", "state"])
    op.execute(
        "CREATE INDEX ix_events_track ON events (track_id) WHERE track_id IS NOT NULL"
    )
    op.create_index("ix_track_attrs_track", "track_attributes", ["local_track_id"])
    op.create_index("ix_audit_logs_user", "audit_logs", ["user_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"])


def downgrade() -> None:
    # ------------------------------------------------------------------
    # Reverse: drop indexes, remove policies, drop tables
    # ------------------------------------------------------------------

    # Drop relational indexes
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user", table_name="audit_logs")
    op.drop_index("ix_track_attrs_track", table_name="track_attributes")
    op.execute("DROP INDEX IF EXISTS ix_events_track")
    op.drop_index("ix_events_type_state", table_name="events")
    op.drop_index("ix_events_camera_time", table_name="events")
    op.drop_index("ix_global_track_links_local", table_name="global_track_links")
    op.drop_index("ix_global_track_links_global", table_name="global_track_links")
    op.drop_index("ix_local_tracks_time_range", table_name="local_tracks")
    op.drop_index("ix_local_tracks_camera_state", table_name="local_tracks")
    op.drop_index("ix_cameras_site", table_name="cameras")

    # Drop hypertable indexes
    op.drop_index("ix_track_obs_track", table_name="track_observations")
    op.drop_index("ix_track_obs_camera_time", table_name="track_observations")
    op.drop_index("ix_detections_track", table_name="detections")
    op.drop_index("ix_detections_class_time", table_name="detections")
    op.drop_index("ix_detections_camera_time", table_name="detections")

    # Remove TimescaleDB policies
    op.execute("SELECT remove_compression_policy('track_observations', if_exists => TRUE)")
    op.execute("SELECT remove_compression_policy('detections', if_exists => TRUE)")
    op.execute("SELECT remove_retention_policy('track_observations', if_exists => TRUE)")
    op.execute("SELECT remove_retention_policy('detections', if_exists => TRUE)")

    # Drop all tables in reverse dependency order
    op.drop_table("track_observations")
    op.drop_table("detections")
    op.drop_table("audit_logs")
    op.drop_table("events")
    op.drop_table("track_attributes")
    op.drop_table("global_track_links")
    op.drop_table("global_tracks")
    op.drop_table("local_tracks")
    op.drop_table("topology_edges")
    op.drop_table("cameras")
    op.drop_table("users")
    op.drop_table("sites")
