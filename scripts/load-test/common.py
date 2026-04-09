#!/usr/bin/env python3
"""Shared helpers for the end-to-end stress-test harness."""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT_S = 15.0
_PROTO_TEMP_DIR: tempfile.TemporaryDirectory[str] | None = None


def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    """Return a compact RFC3339 UTC string."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_hs256_jwt(payload: dict[str, Any], secret: str) -> str:
    """Build an HS256 JWT using only the Python standard library."""
    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    encoded_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    signature = hmac.new(
        secret.encode(),
        signing_input,
        digestmod=hashlib.sha256,
    ).digest()
    encoded_signature = _b64url_encode(signature)
    return f"{encoded_header}.{encoded_payload}.{encoded_signature}"


def build_query_headers(
    *,
    secret: str,
    cookie_name: str,
    role: str,
    camera_scope: list[str] | None,
    user_id: str = "stress-test-user",
    username: str = "stress-test-user",
) -> dict[str, str]:
    """Return request headers carrying a valid Query API cookie."""
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "camera_scope": camera_scope or [],
    }
    token = build_hs256_jwt(payload, secret)
    return {
        "Cookie": f"{cookie_name}={token}",
        "Accept": "application/json",
        "User-Agent": "cilex-stress-test/1.0",
    }


def http_get_json(
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Any:
    """Fetch a JSON response with urllib."""
    query = urlencode(params or {})
    target_url = f"{url}?{query}" if query else url
    request = Request(target_url, headers=headers or {}, method="GET")
    with urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


def load_frame_ref_type() -> type[Any]:
    """Import the generated ``FrameRef`` protobuf, building it on demand."""
    for candidate in _proto_search_paths():
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
        try:
            frame_pb2 = importlib.import_module("vidanalytics.v1.frame.frame_pb2")
            return frame_pb2.FrameRef
        except ModuleNotFoundError:
            continue
    generated_dir = _generate_frame_proto()
    if str(generated_dir) not in sys.path:
        sys.path.insert(0, str(generated_dir))
    frame_pb2 = importlib.import_module("vidanalytics.v1.frame.frame_pb2")
    return frame_pb2.FrameRef


def set_proto_timestamp(field: Any, epoch_s: float) -> None:
    """Populate a protobuf ``Timestamp`` field from epoch seconds."""
    whole_seconds = int(epoch_s)
    field.seconds = whole_seconds
    field.nanos = int((epoch_s - whole_seconds) * 1_000_000_000)


def load_cost_model_params(path: Path) -> dict[str, Any]:
    """Read the cost-model YAML document."""
    import yaml  # noqa: PLC0415

    with path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected mapping payload in {path}")
    return payload


def _proto_search_paths() -> list[Path]:
    return [
        REPO_ROOT / "services" / "decode-service" / "proto_gen",
        REPO_ROOT / "services" / "inference-worker" / "proto_gen",
        REPO_ROOT / "services" / "ingress-bridge" / "proto_gen",
        REPO_ROOT / "services" / "clip-service" / "proto_gen",
    ]


def _generate_frame_proto() -> Path:
    global _PROTO_TEMP_DIR
    if _PROTO_TEMP_DIR is None:
        _PROTO_TEMP_DIR = tempfile.TemporaryDirectory(prefix="cilex-stress-proto-")
    output_dir = Path(_PROTO_TEMP_DIR.name)

    try:
        from grpc_tools import protoc  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "generated FrameRef protobufs are unavailable; run `bash services/"
            "decode-service/gen_proto.sh` or install grpcio-tools"
        ) from exc

    proto_root = REPO_ROOT / "proto"
    common_root = REPO_ROOT
    frame_proto = proto_root / "vidanalytics" / "v1" / "frame" / "frame.proto"
    result = protoc.main(
        (
            "grpc_tools.protoc",
            f"-I{proto_root}",
            f"-I{common_root}",
            f"--python_out={output_dir}",
            str(frame_proto),
        )
    )
    if result != 0:
        raise RuntimeError("failed to generate FrameRef protobufs")
    return output_dir


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
