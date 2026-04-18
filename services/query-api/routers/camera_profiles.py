"""CRUD for camera recording profiles."""

from __future__ import annotations

import json
from datetime import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth.jwt import get_current_user
from schemas import UserClaims
from utils.db import fetch_rows

router = APIRouter(prefix="/admin/camera-profiles", tags=["camera_profiles"])


class CameraProfile(BaseModel):
    profile_id: str
    name: str
    description: Optional[str] = None
    recording_mode: str
    business_hours_start: Optional[str] = None
    business_hours_end: Optional[str] = None
    business_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    motion_sensitivity: float = 0.5
    pre_roll_s: int = 5
    post_roll_s: int = 5
    timezone: str = "UTC"
    is_default: bool = False
    cameras_assigned: int = 0


class ProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    recording_mode: str = "continuous"
    business_hours_start: Optional[str] = None
    business_hours_end: Optional[str] = None
    business_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    motion_sensitivity: float = 0.5
    pre_roll_s: int = 5
    post_roll_s: int = 5
    timezone: str = "UTC"


class AssignProfileRequest(BaseModel):
    profile_id: str


def _require_admin(user: UserClaims) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


def _parse_time(value: Optional[str]) -> Optional[time]:
    if not value:
        return None
    parts = value.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return time(h, m, s)
    except (ValueError, IndexError):
        raise HTTPException(400, f"Invalid time: {value}")


def _parse_business_days(raw) -> list[int]:
    if raw is None:
        return [1, 2, 3, 4, 5]
    if isinstance(raw, list):
        return [int(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [int(x) for x in parsed]
        except json.JSONDecodeError:
            pass
    return [1, 2, 3, 4, 5]


@router.get("")
async def list_profiles(
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    _require_admin(user)
    pool = request.app.state.db_pool
    rows = await fetch_rows(
        pool,
        """
        SELECT p.profile_id, p.name, p.description, p.recording_mode,
               p.business_hours_start, p.business_hours_end, p.business_days,
               p.motion_sensitivity, p.pre_roll_s, p.post_roll_s, p.timezone,
               p.is_default,
               (SELECT COUNT(*) FROM cameras WHERE profile_id = p.profile_id) AS cameras_assigned
        FROM camera_profiles p
        ORDER BY p.is_default DESC, p.name
        """,
        [],
        query_type="list_profiles",
    )

    return {
        "profiles": [
            CameraProfile(
                profile_id=str(r["profile_id"]),
                name=r["name"],
                description=r["description"],
                recording_mode=r["recording_mode"],
                business_hours_start=str(r["business_hours_start"]) if r["business_hours_start"] else None,
                business_hours_end=str(r["business_hours_end"]) if r["business_hours_end"] else None,
                business_days=_parse_business_days(r["business_days"]),
                motion_sensitivity=float(r["motion_sensitivity"]),
                pre_roll_s=int(r["pre_roll_s"]),
                post_roll_s=int(r["post_roll_s"]),
                timezone=r["timezone"],
                is_default=bool(r["is_default"]),
                cameras_assigned=int(r["cameras_assigned"]),
            )
            for r in rows
        ]
    }


@router.post("")
async def create_profile(
    body: ProfileCreate,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    _require_admin(user)
    pool = request.app.state.db_pool

    row = await fetch_rows(
        pool,
        """
        INSERT INTO camera_profiles
            (name, description, recording_mode, business_hours_start, business_hours_end,
             business_days, motion_sensitivity, pre_roll_s, post_roll_s, timezone)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
        RETURNING profile_id
        """,
        [
            body.name, body.description, body.recording_mode,
            _parse_time(body.business_hours_start),
            _parse_time(body.business_hours_end),
            json.dumps(body.business_days),
            body.motion_sensitivity, body.pre_roll_s, body.post_roll_s, body.timezone,
        ],
        query_type="create_profile",
    )

    return {"profile_id": str(row[0]["profile_id"])}


@router.get("/assignments")
async def list_assignments(
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    """Map of camera_id → assigned profile (for the cameras admin page)."""
    _require_admin(user)
    pool = request.app.state.db_pool
    rows = await fetch_rows(
        pool,
        """
        SELECT c.camera_id, p.profile_id, p.name AS profile_name, p.recording_mode
        FROM cameras c
        LEFT JOIN camera_profiles p ON p.profile_id = c.profile_id
        ORDER BY c.camera_id
        """,
        [],
        query_type="list_assignments",
    )
    return {
        "assignments": [
            {
                "camera_id": r["camera_id"],
                "profile_id": str(r["profile_id"]) if r["profile_id"] else None,
                "profile_name": r["profile_name"],
                "recording_mode": r["recording_mode"],
            }
            for r in rows
        ]
    }


@router.put("/assign/{camera_id}")
async def assign_profile_to_camera(
    camera_id: str,
    body: AssignProfileRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    _require_admin(user)
    pool = request.app.state.db_pool
    await fetch_rows(
        pool,
        "UPDATE cameras SET profile_id = $1::uuid WHERE camera_id = $2",
        [body.profile_id, camera_id],
        query_type="assign_profile",
    )
    return {"status": "ok"}


@router.put("/{profile_id}")
async def update_profile(
    profile_id: str,
    body: ProfileCreate,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    _require_admin(user)
    pool = request.app.state.db_pool

    await fetch_rows(
        pool,
        """
        UPDATE camera_profiles SET
            name = $2, description = $3, recording_mode = $4,
            business_hours_start = $5, business_hours_end = $6,
            business_days = $7::jsonb, motion_sensitivity = $8,
            pre_roll_s = $9, post_roll_s = $10, timezone = $11,
            updated_at = NOW()
        WHERE profile_id = $1::uuid
        """,
        [
            profile_id, body.name, body.description, body.recording_mode,
            _parse_time(body.business_hours_start),
            _parse_time(body.business_hours_end),
            json.dumps(body.business_days),
            body.motion_sensitivity, body.pre_roll_s, body.post_roll_s, body.timezone,
        ],
        query_type="update_profile",
    )
    return {"status": "ok"}


@router.delete("/{profile_id}")
async def delete_profile(
    profile_id: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
):
    _require_admin(user)
    pool = request.app.state.db_pool

    chk = await fetch_rows(
        pool,
        "SELECT is_default, (SELECT COUNT(*) FROM cameras WHERE profile_id = $1::uuid) AS in_use "
        "FROM camera_profiles WHERE profile_id = $1::uuid",
        [profile_id],
        query_type="check_profile",
    )
    if not chk:
        raise HTTPException(404, "Profile not found")
    if chk[0]["is_default"]:
        raise HTTPException(400, "Cannot delete the default profile")
    if chk[0]["in_use"] > 0:
        raise HTTPException(400, f"Profile is assigned to {chk[0]['in_use']} cameras")

    await fetch_rows(
        pool,
        "DELETE FROM camera_profiles WHERE profile_id = $1::uuid",
        [profile_id],
        query_type="delete_profile",
    )
    return {"status": "ok"}
