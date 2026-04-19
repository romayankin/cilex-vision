"""POST /search/nlp — AI natural language search using local Ollama.

Loads camera/zone context, calls Gemma 2 2B with a prompt describing the
v1 metadata_jsonb shape, validates the parsed filter spec against known
enums, and runs the resulting query via the shared parameterized query
builder. Returns the actual matching events (not filter params).
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
from routers.events import build_metadata_filter_sql
from schemas import UserClaims
from utils.db import fetch_rows

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

OLLAMA_BASE_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma2:2b")
OLLAMA_TIMEOUT_S = float(os.environ.get("OLLAMA_TIMEOUT", "60"))

VALID_CLASSES = {"person", "car", "truck", "animal"}
VALID_COLORS = {
    "black", "white", "red", "blue", "green", "yellow",
    "silver", "orange", "brown", "gray",
}


class NlpSearchRequest(BaseModel):
    query: str


def _build_system_prompt(cameras: list[dict[str, Any]], now: datetime) -> str:
    """System prompt describing the v1 metadata_jsonb filter shape.

    Gemma outputs filter fields that map cleanly to the shared
    build_metadata_filter_sql helper (camera_ids, time range,
    contains_classes, colors, duration bounds).
    """
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
    week_dates = {
        name: (monday + timedelta(days=i)).isoformat()
        for i, name in enumerate(
            ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        )
    }
    yesterday = (today - timedelta(days=1)).isoformat()
    today_iso = today.isoformat()

    return f"""You parse surveillance search queries into JSON filters for a motion-event database.

CURRENT: {now.strftime('%A %Y-%m-%d %H:%M UTC')}
THIS WEEK: Monday={week_dates['Monday']}, Tuesday={week_dates['Tuesday']}, Wednesday={week_dates['Wednesday']}, Thursday={week_dates['Thursday']}, Friday={week_dates['Friday']}, Saturday={week_dates['Saturday']}, Sunday={week_dates['Sunday']}
TODAY: {today_iso}
YESTERDAY: {yesterday}

CAMERAS:
{cameras_text}

ZONE MATCHING: If the user mentions a zone name (e.g. "office", "entrance"), find the camera that has that zone and set camera_ids accordingly.

CLASSES supported: person, car, truck, animal
COLORS supported: black, white, red, blue, green, yellow, silver, orange, brown, gray

TIME RANGES:
  morning   = 06:00-12:00
  afternoon = 12:00-17:00
  evening   = 17:00-22:00
  night     = 22:00-06:00 (next day)

DATETIMES: Always full ISO 8601 with +00:00. Example: "{yesterday}T12:00:00+00:00"

OUTPUT SHAPE (return ONLY JSON, no prose, no markdown):
{{
  "camera_ids": ["cam-1"] or [],
  "time_start": "ISO8601" or null,
  "time_end":   "ISO8601" or null,
  "contains_classes": ["person"] or [],
  "colors": ["red"] or [],
  "min_duration_s": 30 or null,
  "max_duration_s": null,
  "explanation": "human-readable summary of what you filtered"
}}

EXAMPLES:

Query: "red car in cam-1 this morning"
{{"camera_ids":["cam-1"],"time_start":"{today_iso}T06:00:00+00:00","time_end":"{today_iso}T12:00:00+00:00","contains_classes":["car"],"colors":["red"],"min_duration_s":null,"max_duration_s":null,"explanation":"Red car seen on cam-1 between 06:00 and 12:00 today"}}

Query: "person yesterday evening"
{{"camera_ids":[],"time_start":"{yesterday}T17:00:00+00:00","time_end":"{yesterday}T22:00:00+00:00","contains_classes":["person"],"colors":[],"min_duration_s":null,"max_duration_s":null,"explanation":"Person seen yesterday between 17:00 and 22:00 across all cameras"}}

Query: "motion lasting more than 2 minutes"
{{"camera_ids":[],"time_start":null,"time_end":null,"contains_classes":[],"colors":[],"min_duration_s":120,"max_duration_s":null,"explanation":"Motion events longer than 2 minutes (any camera, any time)"}}

Query: "activity in office zone today"
{{"camera_ids":["cam-2"],"time_start":"{today_iso}T00:00:00+00:00","time_end":null,"contains_classes":[],"colors":[],"min_duration_s":null,"max_duration_s":null,"explanation":"Any motion events in cam-2 (which has the office zone) today"}}

Now parse this query:"""


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


def _parse_dt(val: Any) -> datetime | None:
    if not isinstance(val, str) or len(val) < 10:
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_number(val: Any) -> float | None:
    if val is None:
        return None
    try:
        n = float(val)
        if n < 0 or n > 86400:
            return None
        return n
    except (TypeError, ValueError):
        return None


@router.post("/nlp")
async def nlp_search(
    body: NlpSearchRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Parse a NL query into filters, run the query, return events."""
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
    except httpx.ConnectError as exc:
        raise HTTPException(
            503,
            "AI search unavailable — Ollama is not running. "
            "Enable it in /admin/services.",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            504,
            "AI model took too long to respond. The model may be loading "
            "into memory (first request after idle). Try again.",
        ) from exc

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
            "events": [],
            "total": 0,
            "explanation": (
                "I couldn't parse that query. Try being more specific — "
                "for example: 'red car on cam-1 yesterday morning'."
            ),
            "filters_used": {},
            "raw_query": query,
            "parse_error": True,
        }

    valid_cameras = {c["camera_id"] for c in cameras}
    camera_ids = [c for c in (parsed.get("camera_ids") or []) if c in valid_cameras]
    contains_classes = [c for c in (parsed.get("contains_classes") or []) if c in VALID_CLASSES]
    colors = [c for c in (parsed.get("colors") or []) if c in VALID_COLORS]

    time_start = _parse_dt(parsed.get("time_start"))
    time_end = _parse_dt(parsed.get("time_end"))
    min_duration_s = _parse_number(parsed.get("min_duration_s"))
    max_duration_s = _parse_number(parsed.get("max_duration_s"))

    filters_used = {
        "camera_ids": camera_ids,
        "time_start": time_start.isoformat() if time_start else None,
        "time_end": time_end.isoformat() if time_end else None,
        "contains_classes": contains_classes,
        "colors": colors,
        "min_duration_s": min_duration_s,
        "max_duration_s": max_duration_s,
    }

    sql, args = build_metadata_filter_sql(
        camera_ids=camera_ids or None,
        start=time_start,
        end=time_end,
        contains_classes=contains_classes or None,
        colors=colors or None,
        min_duration_s=min_duration_s,
        max_duration_s=max_duration_s,
        limit=50,
        offset=0,
    )

    rows = await fetch_rows(pool, sql, args, query_type="nlp_search")

    def _decode_metadata(raw: Any) -> Any:
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
        return raw

    events = []
    for r in rows:
        events.append({
            "event_id": str(r["event_id"]),
            "camera_id": r["camera_id"],
            "start_time": r["start_time"].isoformat(),
            "end_time": r["end_time"].isoformat() if r["end_time"] else None,
            "duration_ms": r["duration_ms"],
            "state": r["state"],
            "clip_uri": r["clip_uri"],
            "clip_source_type": r["clip_source_type"],
            "metadata": _decode_metadata(r["metadata_jsonb"]),
        })

    return {
        "events": events,
        "total": len(events),
        "explanation": parsed.get("explanation", ""),
        "filters_used": filters_used,
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
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e)}
