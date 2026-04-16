"""POST /search/nlp — AI natural language search using local Ollama.

Loads camera/zone context, calls Ollama's OpenAI-compatible API with
Gemma 2 2B to parse the query into structured filters.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

OLLAMA_BASE_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma2:2b")
OLLAMA_TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT", "60"))


class NlpSearchRequest(BaseModel):
    query: str


def _build_system_prompt(cameras: list[dict[str, Any]], now: datetime) -> str:
    """Build system prompt with camera/zone context for the LLM."""
    camera_lines = []
    for cam in cameras:
        desc = cam.get("location_description") or "no description"
        zones = cam.get("zones", [])
        zone_names = [z["name"] for z in zones if z.get("name")] or ["no named zones"]
        camera_lines.append(
            f"  - {cam['camera_id']} ({cam['name']}): {desc}. "
            f"Zones: {', '.join(zone_names)}"
        )

    cameras_text = "\n".join(camera_lines) if camera_lines else "  No cameras configured."

    return f"""You are a search query parser for a video surveillance system.
Your ONLY job: take the user's natural language query and return a JSON object with structured filters.

CURRENT DATE/TIME: {now.strftime('%A, %B %d, %Y %H:%M UTC')}

CAMERAS:
{cameras_text}

OBJECT CLASSES (use exactly): person, car, truck, bus, bicycle, motorcycle, animal
EVENT TYPES (use exactly): entered_scene, exited_scene, loitering, stopped
COLORS (use exactly): black, white, red, blue, green, yellow, silver, orange, brown

TIME CONVENTIONS:
- "morning" = 06:00-12:00
- "afternoon" = 12:00-17:00
- "evening" = 17:00-22:00
- "night" = 22:00-06:00
- "yesterday" = the day before today
- "Monday" = most recent Monday (past, not future)

RULES:
1. Map location mentions to the correct camera using descriptions and zone names.
2. Convert relative dates to ISO datetimes based on the current date/time.
3. Only set fields you are confident about. Leave others as empty strings.
4. Respond with ONLY a JSON object. No markdown, no backticks, no other text.

JSON FORMAT:
{{"camera_id":"","object_class":"","event_type":"","start":"","end":"","zone_name":"","color":"","state":"","explanation":"one sentence describing what you understood"}}"""


async def _call_ollama(system_prompt: str, user_query: str) -> str:
    """Call Ollama's OpenAI-compatible chat endpoint."""
    url = f"{OLLAMA_BASE_URL}/v1/chat/completions"

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }

    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT_S) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code != 200:
        logger.error("Ollama returned %d: %s", resp.status_code, resp.text[:500])
        raise HTTPException(502, f"AI model error: HTTP {resp.status_code}")

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise HTTPException(502, "AI model returned no response")

    return choices[0].get("message", {}).get("content", "")


async def _load_camera_context(pool: Any) -> list[dict[str, Any]]:
    """Load camera + zone metadata from the database."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT camera_id, name, location_description, config_json "
            "FROM cameras ORDER BY camera_id"
        )

    cameras: list[dict[str, Any]] = []
    for row in rows:
        config = row["config_json"] or {}
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        zones: list[dict[str, Any]] = []
        for z in config.get("loitering_zones") or []:
            if not isinstance(z, dict):
                continue
            zones.append({
                "zone_id": z.get("zone_id", ""),
                "name": z.get("name", ""),
            })
        cameras.append({
            "camera_id": row["camera_id"],
            "name": row["name"],
            "location_description": row["location_description"] or "",
            "zones": zones,
        })
    return cameras


@router.post("/nlp")
async def nlp_search(
    body: NlpSearchRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Parse a natural language search query into structured filters."""
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "Query cannot be empty")
    if len(query) > 500:
        raise HTTPException(400, "Query too long (max 500 characters)")

    pool = request.app.state.db_pool
    cameras = await _load_camera_context(pool)

    now = datetime.now(timezone.utc)
    system_prompt = _build_system_prompt(cameras, now)

    try:
        raw_text = await _call_ollama(system_prompt, query)
    except httpx.ConnectError:
        raise HTTPException(
            503,
            "AI search unavailable — Ollama is not running. "
            "Check the ollama container in /admin/services.",
        )
    except httpx.TimeoutException:
        raise HTTPException(
            504,
            "AI model took too long to respond. "
            "The model may be loading into memory (first request after idle). Try again.",
        )

    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Ollama returned non-JSON: %s", text[:300])
        return {
            "filters": {},
            "explanation": (
                "I couldn't parse that query. Try being more specific, "
                "for example: 'person entering server room Monday morning'"
            ),
            "raw_query": query,
            "parse_error": True,
        }

    valid_classes = {"person", "car", "truck", "bus", "bicycle", "motorcycle", "animal"}
    valid_events = {"entered_scene", "exited_scene", "loitering", "stopped"}
    valid_cameras = {cam["camera_id"] for cam in cameras}
    valid_colors = {"black", "white", "red", "blue", "green", "yellow", "silver", "orange", "brown"}

    filters: dict[str, str] = {}
    if parsed.get("camera_id") and parsed["camera_id"] in valid_cameras:
        filters["camera_id"] = parsed["camera_id"]
    if parsed.get("object_class") and parsed["object_class"] in valid_classes:
        filters["object_class"] = parsed["object_class"]
    if parsed.get("event_type") and parsed["event_type"] in valid_events:
        filters["event_type"] = parsed["event_type"]
    if parsed.get("start"):
        filters["start"] = parsed["start"]
    if parsed.get("end"):
        filters["end"] = parsed["end"]
    if parsed.get("color") and parsed["color"] in valid_colors:
        filters["color"] = parsed["color"]
    if parsed.get("state"):
        filters["state"] = parsed["state"]

    return {
        "filters": filters,
        "explanation": parsed.get("explanation", ""),
        "zone_name": parsed.get("zone_name", ""),
        "raw_query": query,
    }


@router.get("/nlp/status")
async def nlp_status(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Check if AI search is available (Ollama running + model pulled)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        if resp.status_code != 200:
            return {"available": False, "reason": "Ollama not responding"}

        data = resp.json()
        models = [m.get("name", "") for m in data.get("models", [])]
        model_base = OLLAMA_MODEL.split(":")[0]
        has_model = any(model_base in m for m in models)

        if not has_model:
            return {
                "available": False,
                "reason": f"Model {OLLAMA_MODEL} not pulled yet. Wait for ollama-init to finish.",
                "models": models,
            }

        return {"available": True, "model": OLLAMA_MODEL, "models": models}
    except httpx.ConnectError:
        return {"available": False, "reason": "Ollama container not running"}
    except Exception as e:
        return {"available": False, "reason": str(e)}
