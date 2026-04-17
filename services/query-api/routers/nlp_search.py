"""POST /search/nlp — AI natural language search using local Ollama.

Loads camera/zone context, calls Ollama's OpenAI-compatible API with
Gemma 2 2B to parse the query into structured filters.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
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
            f"  - {cam['camera_id']}: location={desc}, zones=[{', '.join(zone_names)}]"
        )

    cameras_text = "\n".join(camera_lines) if camera_lines else "  No cameras configured."

    today = now.date()
    weekday = today.weekday()
    monday = today - timedelta(days=weekday)
    week_dates = {}
    for i, name in enumerate(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    ):
        d = monday + timedelta(days=i)
        week_dates[name] = d.isoformat()

    week_ref = ", ".join(f"{name}={date}" for name, date in week_dates.items())
    yesterday = (today - timedelta(days=1)).isoformat()

    return f"""You parse surveillance search queries into JSON filters.

CURRENT: {now.strftime('%A %Y-%m-%d %H:%M UTC')}
THIS WEEK: {week_ref}

CAMERAS:
{cameras_text}

ZONE MATCHING RULE: If the user mentions a zone name (e.g. "office"), find the camera that has that zone. Zone names are the PRIMARY key — ignore location descriptions when a zone name matches.

CLASSES: person, car
EVENTS: entered_scene, exited_scene, loitering, stopped
COLORS: black, white, red, blue, green, yellow, silver, orange, brown

DATETIME FORMAT: Always use full ISO 8601 with timezone. Example: "2026-04-14T06:00:00+00:00"
Never return partial times like "16:30" or dates like "Monday".

TIME RANGES: morning=06:00-12:00, afternoon=12:00-17:00, evening=17:00-22:00, night=22:00-06:00

Return ONLY a JSON object. No other text.

EXAMPLES:

Query: "person in the office friday afternoon"
{{"camera_id":"cam-2","object_class":"person","event_type":"","start":"{week_dates['Friday']}T12:00:00+00:00","end":"{week_dates['Friday']}T17:00:00+00:00","zone_name":"office","color":"","explanation":"Person in office zone (cam-2) on Friday afternoon"}}

Query: "cars yesterday evening"
{{"camera_id":"","object_class":"car","event_type":"","start":"{yesterday}T17:00:00+00:00","end":"{yesterday}T22:00:00+00:00","zone_name":"","color":"","explanation":"Cars detected yesterday evening across all cameras"}}

Query: "red car in parking lot"
{{"camera_id":"","object_class":"car","event_type":"","start":"","end":"","zone_name":"","color":"red","explanation":"Red car detected anywhere (no time filter)"}}

Now parse this query:"""


def _validate_datetime(value: str) -> str | None:
    """Return a valid ISO datetime string or None if it can't be parsed as one."""
    if not value or len(value) < 10:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat()
    except (ValueError, TypeError):
        return None


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
        validated = _validate_datetime(parsed["start"])
        if validated:
            filters["start"] = validated
    if parsed.get("end"):
        validated = _validate_datetime(parsed["end"])
        if validated:
            filters["end"] = validated
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
