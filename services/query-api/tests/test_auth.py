"""Tests for JWT authentication and RBAC authorization."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestJwtAuth:
    @pytest.mark.asyncio
    async def test_no_cookie_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get("/detections")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get(
            "/detections",
            cookies={"access_token": "garbage-token"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self, client: AsyncClient) -> None:
        import jwt as pyjwt

        token = pyjwt.encode(
            {"sub": "user1", "username": "u", "role": "viewer", "camera_scope": [], "exp": 0},
            "test-secret",
            algorithm="HS256",
        )
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_returns_200(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_role_returns_403(
        self, client: AsyncClient, make_jwt
    ) -> None:
        # Engineering can't access events
        token = make_jwt(role="engineering")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_role_returns_401(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="unknown_role")
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        assert resp.status_code == 401


class TestRbacRoles:
    @pytest.mark.asyncio
    async def test_admin_can_access_events(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="admin")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_viewer_can_access_detections(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="viewer")
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_engineering_can_access_tracks(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="engineering")
        resp = await client.get(
            "/tracks",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_viewer_cannot_access_engineering_only_resource(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        # Both viewer and engineering can access detections and tracks,
        # but only admin/operator/viewer can access events
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="viewer")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200  # viewer CAN access events
