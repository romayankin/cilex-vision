"""JWT authentication from httpOnly cookies.

Per security-design.md:
- JWT is delivered via httpOnly cookie (not Authorization header)
- 4 RBAC roles: admin, operator, viewer, engineering
- Camera scope filtering: users see only cameras in their scope

Admin role bypasses camera scope (sees all cameras).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from config import Settings
from metrics import AUTH_FAILURES
from schemas import UserClaims

logger = logging.getLogger(__name__)

# RBAC permission matrix (from privacy-framework.md draft):
#   admin      — all endpoints, audit logs, all cameras
#   operator   — detections, tracks, events (scoped cameras)
#   viewer     — detections, tracks, events (scoped cameras, read-only)
#   engineering — detections, tracks (scoped cameras, for debugging)
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"detections", "tracks", "events", "audit"},
    "operator": {"detections", "tracks", "events"},
    "viewer": {"detections", "tracks", "events"},
    "engineering": {"detections", "tracks"},
}

ALL_ROLES = set(ROLE_PERMISSIONS.keys())


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _decode_token(token: str, settings: Settings) -> dict[str, Any]:
    """Decode and verify a JWT token."""
    try:
        import jwt as pyjwt  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "missing optional dependency 'PyJWT'; install requirements.txt"
        ) from exc

    try:
        payload = pyjwt.decode(
            token,
            settings.jwt.secret_key,
            algorithms=[settings.jwt.algorithm],
        )
        return payload
    except pyjwt.ExpiredSignatureError:
        AUTH_FAILURES.labels(reason="expired").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except pyjwt.InvalidTokenError:
        AUTH_FAILURES.labels(reason="invalid_token").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


def get_current_user(request: Request) -> UserClaims:
    """Extract and validate JWT from httpOnly cookie.

    Returns UserClaims with user identity and camera scope.
    """
    settings = _get_settings(request)
    token = request.cookies.get(settings.jwt.cookie_name)
    if not token:
        AUTH_FAILURES.labels(reason="missing_cookie").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    payload = _decode_token(token, settings)

    user_id = payload.get("sub")
    username = payload.get("username", "")
    role = payload.get("role", "")
    camera_scope = payload.get("camera_scope", [])

    if not user_id or role not in ALL_ROLES:
        AUTH_FAILURES.labels(reason="invalid_claims").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token claims",
        )

    return UserClaims(
        user_id=str(user_id),
        username=username,
        role=role,
        camera_scope=camera_scope if isinstance(camera_scope, list) else [],
    )


def require_role(*allowed_roles: str):
    """Dependency factory that checks the user's role against allowed roles.

    Usage::

        @router.get("/detections", dependencies=[Depends(require_role("admin", "operator", "viewer"))])
    """

    def _check(user: UserClaims = Depends(get_current_user)) -> UserClaims:
        if user.role not in allowed_roles:
            AUTH_FAILURES.labels(reason="forbidden_role").inc()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' not authorized for this resource",
            )
        return user

    return Depends(_check)


def get_camera_filter(user: UserClaims) -> list[str] | None:
    """Return the camera scope for SQL WHERE filtering.

    Returns None for admin (no filtering), or list of camera_ids for
    scoped users.
    """
    if user.role == "admin":
        return None  # admin sees all
    return user.camera_scope
