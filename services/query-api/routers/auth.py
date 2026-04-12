"""POST /auth/login — issue JWT token as httpOnly cookie.

Dev/pilot implementation. Production should use an external IdP.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Dev user table — production should validate against the DB ``users`` table.
_DEV_USERS: dict[str, dict[str, str | list[str]]] = {
    "admin": {"password": "admin", "role": "admin", "camera_scope": []},
    "operator": {"password": "operator", "role": "operator", "camera_scope": []},
    "viewer": {"password": "viewer", "role": "viewer", "camera_scope": []},
}


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str
    role: str


class MeResponse(BaseModel):
    user_id: str
    username: str
    role: str


def _make_token(payload: dict, secret: str, algorithm: str) -> str:
    import jwt as pyjwt  # noqa: PLC0415

    return pyjwt.encode(payload, secret, algorithm=algorithm)


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    dev_user = _DEV_USERS.get(body.username)
    if dev_user is None or dev_user["password"] != body.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    settings = request.app.state.settings
    role = str(dev_user["role"])
    camera_scope = dev_user.get("camera_scope", [])

    payload = {
        "sub": str(uuid.uuid4()),
        "username": body.username,
        "role": role,
        "camera_scope": camera_scope,
        "exp": datetime.now(tz=UTC) + timedelta(hours=24),
        "iat": datetime.now(tz=UTC),
    }
    token = _make_token(payload, settings.jwt.secret_key, settings.jwt.algorithm)

    # httpOnly JWT cookie — not readable by JS
    response.set_cookie(
        key=settings.jwt.cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
        path="/",
    )
    # Non-httpOnly role hint — readable by frontend JS for UI role checks
    response.set_cookie(
        key="user_role",
        value=role,
        httponly=False,
        samesite="lax",
        max_age=86400,
        path="/",
    )

    return LoginResponse(username=body.username, role=role)


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("user_role", path="/")
    return {"status": "ok"}


@router.get("/me", response_model=MeResponse)
async def me(request: Request) -> MeResponse:
    import jwt as pyjwt  # noqa: PLC0415

    settings = request.app.state.settings
    token = request.cookies.get(settings.jwt.cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        payload = pyjwt.decode(token, settings.jwt.secret_key, algorithms=[settings.jwt.algorithm])
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    return MeResponse(
        user_id=payload.get("sub", ""),
        username=payload.get("username", ""),
        role=payload.get("role", ""),
    )
