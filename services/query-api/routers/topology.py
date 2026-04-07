"""FastAPI router for camera topology CRUD.

Endpoints:
- GET    /topology/{site_id}                   — full topology graph
- PUT    /topology/{site_id}/edges             — create / update an edge
- POST   /topology/{site_id}/cameras           — add a camera to a site
- DELETE /topology/{site_id}/cameras/{camera_id} — remove a camera

Auth: admin for writes, admin + operator for reads.
DB: asyncpg raw SQL via the ``db_pool`` on ``request.app.state``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from routers.topology_models import (
    CameraCreateRequest,
    CameraNode,
    EdgeCreateRequest,
    TopologyGraph,
    TransitionEdge,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/topology", tags=["topology"])


# ---------------------------------------------------------------------------
# Lightweight auth helpers (mirrors query-api/auth/jwt.py pattern)
# ---------------------------------------------------------------------------


def _get_current_user(request: Request) -> dict[str, Any]:
    """Extract JWT claims from httpOnly cookie.

    Minimal implementation — full RBAC is in query-api/auth/jwt.py.
    When the topology router is mounted in the query-api app, the app's
    auth middleware can be used instead.
    """
    settings = request.app.state.settings
    cookie_name = getattr(settings, "cookie_name", None)
    if cookie_name is None:
        jwt_cfg = getattr(settings, "jwt", None)
        cookie_name = getattr(jwt_cfg, "cookie_name", "access_token")

    token = request.cookies.get(cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    try:
        import jwt as pyjwt  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("missing PyJWT") from exc

    secret = getattr(settings, "secret_key", None)
    if secret is None:
        jwt_cfg = getattr(settings, "jwt", None)
        secret = getattr(jwt_cfg, "secret_key", "change-me")
    algorithm = "HS256"
    jwt_cfg = getattr(settings, "jwt", None)
    if jwt_cfg:
        algorithm = getattr(jwt_cfg, "algorithm", algorithm)

    try:
        payload = pyjwt.decode(token, secret, algorithms=[algorithm])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    role = payload.get("role", "")
    if role not in ("admin", "operator", "viewer", "engineering"):
        raise HTTPException(status_code=401, detail="Invalid role")

    return payload


def _require_admin(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def _require_read(request: Request) -> dict[str, Any]:
    user = _get_current_user(request)
    if user.get("role") not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Insufficient role")
    return user


# ---------------------------------------------------------------------------
# Helper: asyncpg pool from app state
# ---------------------------------------------------------------------------


def _pool(request: Request) -> Any:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return pool


# ---------------------------------------------------------------------------
# GET /topology/{site_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{site_id}",
    response_model=TopologyGraph,
)
async def get_topology(
    request: Request,
    site_id: str,
    user: dict = Depends(_require_read),
) -> TopologyGraph:
    """Return the full topology graph for a site."""
    pool = _pool(request)

    async with pool.acquire() as conn:
        cam_rows = await conn.fetch(
            "SELECT camera_id, site_id, name, latitude, longitude, "
            "status, location_description, config_json "
            "FROM cameras WHERE site_id = $1",
            site_id,
        )
        edge_rows = await conn.fetch(
            "SELECT e.edge_id, e.camera_a_id, e.camera_b_id, "
            "e.transition_time_s, e.confidence, e.enabled "
            "FROM topology_edges e "
            "JOIN cameras ca ON ca.camera_id = e.camera_a_id "
            "WHERE ca.site_id = $1",
            site_id,
        )

    cameras = [
        CameraNode(
            camera_id=r["camera_id"],
            site_id=str(r["site_id"]),
            name=r["name"],
            zone_id=_extract_zone_id(r["config_json"]),
            latitude=r["latitude"],
            longitude=r["longitude"],
            status=r["status"],
            location_description=r["location_description"],
        )
        for r in cam_rows
    ]

    edges = [
        TransitionEdge(
            edge_id=str(r["edge_id"]),
            camera_a_id=r["camera_a_id"],
            camera_b_id=r["camera_b_id"],
            transition_time_s=r["transition_time_s"],
            confidence=r["confidence"],
            enabled=r["enabled"],
            transit_distributions=TransitionEdge.default_distributions(
                r["transition_time_s"]
            ),
        )
        for r in edge_rows
    ]

    return TopologyGraph(site_id=site_id, cameras=cameras, edges=edges)


# ---------------------------------------------------------------------------
# PUT /topology/{site_id}/edges
# ---------------------------------------------------------------------------


@router.put(
    "/{site_id}/edges",
    response_model=TransitionEdge,
    status_code=status.HTTP_200_OK,
)
async def upsert_edge(
    request: Request,
    site_id: str,
    body: EdgeCreateRequest,
    user: dict = Depends(_require_admin),
) -> TransitionEdge:
    """Create or update a topology edge between two cameras.

    If an edge already exists between the same camera pair, its values
    are updated.  Otherwise a new edge is inserted.
    """
    pool = _pool(request)

    async with pool.acquire() as conn:
        # Verify both cameras belong to the site
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM cameras "
            "WHERE camera_id IN ($1, $2) AND site_id = $3",
            body.camera_a_id,
            body.camera_b_id,
            site_id,
        )
        if count < 2:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or both cameras not found in site",
            )

        # Upsert
        row = await conn.fetchrow(
            "INSERT INTO topology_edges "
            "(camera_a_id, camera_b_id, transition_time_s, confidence, enabled) "
            "VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (camera_a_id, camera_b_id) "
            "DO UPDATE SET transition_time_s = EXCLUDED.transition_time_s, "
            "confidence = EXCLUDED.confidence, enabled = EXCLUDED.enabled "
            "RETURNING edge_id, camera_a_id, camera_b_id, "
            "transition_time_s, confidence, enabled",
            body.camera_a_id,
            body.camera_b_id,
            body.transition_time_s,
            body.confidence,
            body.enabled,
        )

    if row is None:
        raise HTTPException(status_code=500, detail="Edge upsert failed")

    return TransitionEdge(
        edge_id=str(row["edge_id"]),
        camera_a_id=row["camera_a_id"],
        camera_b_id=row["camera_b_id"],
        transition_time_s=row["transition_time_s"],
        confidence=row["confidence"],
        enabled=row["enabled"],
        transit_distributions=TransitionEdge.default_distributions(
            row["transition_time_s"]
        ),
    )


# ---------------------------------------------------------------------------
# POST /topology/{site_id}/cameras
# ---------------------------------------------------------------------------


@router.post(
    "/{site_id}/cameras",
    response_model=CameraNode,
    status_code=status.HTTP_201_CREATED,
)
async def add_camera(
    request: Request,
    site_id: str,
    body: CameraCreateRequest,
    user: dict = Depends(_require_admin),
) -> CameraNode:
    """Add a camera to a site."""
    pool = _pool(request)

    config_json = json.dumps({"zone_id": body.zone_id}) if body.zone_id else None

    async with pool.acquire() as conn:
        # Verify site exists
        site_exists = await conn.fetchval(
            "SELECT 1 FROM sites WHERE site_id = $1", site_id
        )
        if not site_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Site not found",
            )

        try:
            await conn.execute(
                "INSERT INTO cameras "
                "(camera_id, site_id, name, latitude, longitude, "
                "location_description, config_json) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
                body.camera_id,
                site_id,
                body.name,
                body.latitude,
                body.longitude,
                body.location_description,
                config_json,
            )
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Camera {body.camera_id} already exists",
                )
            raise

    return CameraNode(
        camera_id=body.camera_id,
        site_id=site_id,
        name=body.name,
        zone_id=body.zone_id,
        latitude=body.latitude,
        longitude=body.longitude,
        status="offline",
        location_description=body.location_description,
    )


# ---------------------------------------------------------------------------
# DELETE /topology/{site_id}/cameras/{camera_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/{site_id}/cameras/{camera_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_camera(
    request: Request,
    site_id: str,
    camera_id: str,
    user: dict = Depends(_require_admin),
) -> None:
    """Remove a camera from a site.

    Also deletes all topology edges that reference the camera.
    """
    pool = _pool(request)

    async with pool.acquire() as conn:
        # Delete edges first (FK constraint)
        await conn.execute(
            "DELETE FROM topology_edges "
            "WHERE camera_a_id = $1 OR camera_b_id = $1",
            camera_id,
        )
        result = await conn.execute(
            "DELETE FROM cameras WHERE camera_id = $1 AND site_id = $2",
            camera_id,
            site_id,
        )

    if result == "DELETE 0":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Camera not found in site",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_zone_id(config_json: Any) -> str | None:
    """Extract zone_id from the cameras.config_json JSONB column."""
    if config_json is None:
        return None
    if isinstance(config_json, str):
        try:
            config_json = json.loads(config_json)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(config_json, dict):
        return config_json.get("zone_id")
    return None
