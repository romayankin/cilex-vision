"""Tests for GET /lpr/results."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient


def _lpr_row(camera_id: str = "cam-1", plate_text: str = "ABC123") -> dict:
    return {
        "result_id": uuid.uuid4(),
        "local_track_id": uuid.uuid4(),
        "camera_id": camera_id,
        "plate_text": plate_text,
        "plate_confidence": 0.91,
        "country_format": "latin-3l-3d",
        "plate_bbox_x": 0.2,
        "plate_bbox_y": 0.65,
        "plate_bbox_w": 0.3,
        "plate_bbox_h": 0.1,
        "detected_at": datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc),
        "model_version": "plate-detector+ocr-1.0.0",
    }


@pytest.mark.asyncio
async def test_lpr_returns_results(
    client: AsyncClient,
    make_jwt,
    fake_pool,
) -> None:
    fake_pool.set_count(1)
    fake_pool.set_rows([_lpr_row()])

    response = await client.get(
        "/lpr/results?plate_text=ABC123",
        cookies={"access_token": make_jwt(role="operator")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["results"][0]["plate_text"] == "ABC123"


@pytest.mark.asyncio
async def test_lpr_exact_prefix_and_wildcard_modes(
    client: AsyncClient,
    make_jwt,
    fake_pool,
) -> None:
    fake_pool.set_count(1)
    fake_pool.set_rows([_lpr_row(plate_text="ABX123")])

    prefix_response = await client.get(
        "/lpr/results?plate_text=AB&match_mode=prefix",
        cookies={"access_token": make_jwt(role="operator")},
    )
    assert prefix_response.status_code == 200

    wildcard_response = await client.get(
        "/lpr/results?plate_text=AB*23&match_mode=wildcard",
        cookies={"access_token": make_jwt(role="operator")},
    )
    assert wildcard_response.status_code == 200


@pytest.mark.asyncio
async def test_lpr_camera_scope_filters_access(
    client: AsyncClient,
    make_jwt,
    fake_pool,
) -> None:
    fake_pool.set_count(0)
    fake_pool.set_rows([])

    response = await client.get(
        "/lpr/results?plate_text=ABC123",
        cookies={"access_token": make_jwt(role="operator", camera_scope=["cam-2"])},
    )

    assert response.status_code == 200
    assert response.json()["results"] == []


@pytest.mark.asyncio
async def test_lpr_viewer_forbidden(client: AsyncClient, make_jwt) -> None:
    response = await client.get(
        "/lpr/results?plate_text=ABC123",
        cookies={"access_token": make_jwt(role="viewer")},
    )
    assert response.status_code == 403
