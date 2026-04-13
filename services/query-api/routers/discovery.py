"""POST /streams/discover — find cameras on the LAN via WS-Discovery (ONVIF).

Ported from cilex-vision-provisioner (app/drivers/hikvision_wsdiscovery.py
and app/drivers/hikvision_http.py). Same algorithm:

1. Send a WS-Discovery Probe to the multicast group 239.255.255.250:3702
   with Types = dn:NetworkVideoTransmitter.
2. Collect ProbeMatch replies for ~3 seconds, parse XAddrs to extract each
   camera's HTTP endpoint.
3. For each discovered IP, GET /ISAPI/System/deviceInfo with HTTP Digest
   auth to collect model, serial, firmware, MAC.
4. Cross-reference with the cameras table to flag already-added cameras.

Caveat: WS-Discovery uses UDP multicast. In Docker bridge networking,
multicast typically does not traverse the docker0 bridge to the physical
LAN. For discovery to actually reach cameras the query-api container
needs host networking (or a dedicated sidecar). The scanner still runs
and returns an empty list gracefully when no replies arrive.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/streams", tags=["streams"])

WS_DISCOVERY_ADDR = ("239.255.255.250", 3702)
ISAPI_DEVICE_INFO = "/ISAPI/System/deviceInfo"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "Qwerty12"
DISCOVERY_TIMEOUT_S = 3.0
HTTP_TIMEOUT_S = 3.0


@dataclass(frozen=True, slots=True)
class ProbeMatch:
    endpoint_reference: str
    xaddrs: tuple[str, ...]
    scopes: tuple[str, ...]


def _build_probe() -> bytes:
    """SOAP envelope probing for ONVIF NetworkVideoTransmitter devices."""
    probe_id = uuid4().hex
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<e:Envelope '
        f'xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        f'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        f'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        f'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f'<e:Header>'
        f'<w:MessageID>uuid:{probe_id}</w:MessageID>'
        f'<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        f'<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
        f'</e:Header>'
        f'<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>'
        f'</e:Envelope>'
    ).encode("utf-8")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_probe_matches(payload: bytes) -> list[ProbeMatch]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    out: list[ProbeMatch] = []
    for match_el in root.iter():
        if _local_name(match_el.tag) != "ProbeMatch":
            continue
        addr: str | None = None
        xaddrs: list[str] = []
        scopes: list[str] = []
        for el in match_el.iter():
            name = _local_name(el.tag)
            text = (el.text or "").strip()
            if name == "Address" and text:
                addr = text
            elif name == "XAddrs" and text:
                xaddrs = [x for x in text.split() if x]
            elif name == "Scopes" and text:
                scopes = [x for x in text.split() if x]
        if addr:
            out.append(ProbeMatch(addr, tuple(xaddrs), tuple(scopes)))
    return out


def _ws_discover_sync(timeout_s: float) -> list[ProbeMatch]:
    """Blocking WS-Discovery probe — run in a thread from async code."""
    probe = _build_probe()
    matches: dict[str, ProbeMatch] = {}
    deadline = time.monotonic() + timeout_s
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 0))
        sock.sendto(probe, WS_DISCOVERY_ADDR)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, _ = sock.recvfrom(65535)
            except (TimeoutError, socket.timeout):
                break
            for m in _parse_probe_matches(payload):
                existing = matches.get(m.endpoint_reference)
                if existing is None:
                    matches[m.endpoint_reference] = m
                else:
                    merged_x = tuple(sorted({*existing.xaddrs, *m.xaddrs}))
                    merged_s = tuple(sorted({*existing.scopes, *m.scopes}))
                    matches[m.endpoint_reference] = ProbeMatch(
                        m.endpoint_reference, merged_x, merged_s
                    )
    return [matches[k] for k in sorted(matches)]


def _extract_ip(xaddr: str) -> str | None:
    """Pull the host out of an XAddrs URL."""
    try:
        parsed = urllib.parse.urlparse(xaddr)
    except ValueError:
        return None
    return parsed.hostname


def _scope_hint(scopes: tuple[str, ...], needle: str) -> str | None:
    """ONVIF scopes look like onvif://www.onvif.org/name/HIKVISION."""
    prefix = f"onvif://www.onvif.org/{needle}/"
    for scope in scopes:
        if scope.startswith(prefix):
            return urllib.parse.unquote(scope[len(prefix):])
    return None


def _find_first_text(root: ET.Element, names: tuple[str, ...]) -> str | None:
    for el in root.iter():
        if _local_name(el.tag) in names:
            text = (el.text or "").strip()
            if text:
                return text
    return None


def _parse_device_info(xml_text: str) -> dict[str, str | None]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}
    return {
        "serial": _find_first_text(root, ("serialNumber", "serialNo")),
        "model": _find_first_text(root, ("model", "deviceModel")),
        "device_name": _find_first_text(root, ("deviceName",)),
        "firmware": _find_first_text(root, ("firmwareVersion", "firmwareVer")),
        "mac": _find_first_text(root, ("macAddress",)),
    }


async def _fetch_device_info(
    client: httpx.AsyncClient,
    ip: str,
    username: str,
    password: str,
) -> dict[str, str | None]:
    url = f"http://{ip}{ISAPI_DEVICE_INFO}"
    try:
        resp = await client.get(url, auth=httpx.DigestAuth(username, password))
        if resp.status_code != 200:
            return {}
        return _parse_device_info(resp.text)
    except httpx.HTTPError as exc:
        logger.debug("ISAPI deviceInfo %s failed: %s", ip, exc)
        return {}


@router.post("/discover")
async def discover_cameras(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Scan the LAN for ONVIF cameras via WS-Discovery + ISAPI probe."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    started = time.monotonic()

    # WS-Discovery is blocking socket I/O — run in a thread.
    probe_matches = await asyncio.to_thread(_ws_discover_sync, DISCOVERY_TIMEOUT_S)

    # Deduplicate by IP across XAddrs and filter out non-IP hosts.
    by_ip: dict[str, ProbeMatch] = {}
    for m in probe_matches:
        for xaddr in m.xaddrs:
            ip = _extract_ip(xaddr)
            if ip:
                by_ip.setdefault(ip, m)

    # Existing cameras in DB for already_added check.
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT camera_id, rtsp_uri FROM cameras")
    existing_by_ip: dict[str, str] = {}
    for r in rows:
        uri = r["rtsp_uri"] or ""
        try:
            host = urllib.parse.urlparse(uri).hostname
        except ValueError:
            host = None
        if host:
            existing_by_ip[host] = r["camera_id"]

    # Probe ISAPI for each discovered IP in parallel.
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        info_tasks = [
            _fetch_device_info(client, ip, DEFAULT_USERNAME, DEFAULT_PASSWORD)
            for ip in by_ip
        ]
        infos = await asyncio.gather(*info_tasks) if info_tasks else []

    cameras: list[dict[str, Any]] = []
    for (ip, match), info in zip(by_ip.items(), infos):
        rtsp_url = (
            f"rtsp://{DEFAULT_USERNAME}:{DEFAULT_PASSWORD}"
            f"@{ip}:554/Streaming/Channels/101"
        )
        manufacturer = _scope_hint(match.scopes, "name")
        hardware = _scope_hint(match.scopes, "hardware")
        cameras.append({
            "ip": ip,
            "model": info.get("model") or hardware or manufacturer,
            "serial": info.get("serial"),
            "firmware": info.get("firmware"),
            "mac": info.get("mac"),
            "device_name": info.get("device_name"),
            "manufacturer": manufacturer,
            "rtsp_url": rtsp_url,
            "already_added": ip in existing_by_ip,
            "existing_camera_id": existing_by_ip.get(ip),
        })

    scan_ms = int((time.monotonic() - started) * 1000)
    return {"cameras": cameras, "scan_time_ms": scan_ms}
