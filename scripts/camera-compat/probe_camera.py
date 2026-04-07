#!/usr/bin/env python3
"""Probe an IP camera over ONVIF and RTSP and emit a JSON compatibility report.

The script has two modes:

1. live probe (default)
   - uses ONVIF for device info, stream profiles, and PTZ/imaging capabilities
   - uses OpenCV RTSP decode for an actual stream-open/frame-read check
2. published-only
   - used to seed the matrix from vendor datasheets when hardware is unavailable

Missing optional runtime dependencies fail fast with clear install hints.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


STATUS_VALUES = ("TESTED-OK", "TESTED-ISSUES", "UNTESTED")


class MissingDependencyError(RuntimeError):
    """Raised when an optional runtime dependency required by the chosen mode is absent."""


@dataclass
class StreamReport:
    name: str
    token: str | None
    resolution: str | None
    codec: str | None
    rtsp_uri: str | None


@dataclass
class CapabilityReport:
    supported: bool | None
    summary: str | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class CameraReport:
    brand: str
    model: str
    firmware: str | None
    status: str
    published_only: bool
    probe_timestamp_utc: str
    onvif: CapabilityReport
    rtsp: CapabilityReport
    h265: CapabilityReport
    dual_stream: CapabilityReport
    triple_stream: CapabilityReport
    ptz: CapabilityReport
    ir_detect: str | None
    ik10: str | None
    smart_codec: str | None
    streams: list[StreamReport]
    sources: list[str]
    notes: list[str]
    matrix_columns: dict[str, str]


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise MissingDependencyError(
            f"missing optional dependency '{module_name}'; install {install_hint}"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand", required=True, help="Vendor or brand name.")
    parser.add_argument("--model", required=True, help="Camera model.")
    parser.add_argument("--host", help="Camera hostname or IP for ONVIF.")
    parser.add_argument("--onvif-port", type=int, default=80, help="ONVIF port.")
    parser.add_argument("--username", help="Camera username.")
    parser.add_argument("--password", help="Camera password.")
    parser.add_argument("--rtsp-url", help="Explicit RTSP URL to test.")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=10.0,
        help="Best-effort timeout hint for RTSP/OpenCV reads.",
    )
    parser.add_argument(
        "--status",
        choices=STATUS_VALUES,
        help="Override the derived status.",
    )
    parser.add_argument("--firmware", help="Known firmware version or note.")
    parser.add_argument("--ik10", help="Published or measured IK10 status.")
    parser.add_argument(
        "--smart-codec",
        help="Published or measured smart codec mode summary.",
    )
    parser.add_argument(
        "--ir-detect",
        help="Published or measured IR/day-night behavior summary.",
    )
    parser.add_argument(
        "--onvif-summary",
        help="Published ONVIF summary for published-only mode or manual override.",
    )
    parser.add_argument(
        "--rtsp-summary",
        help="Published RTSP summary for published-only mode or manual override.",
    )
    parser.add_argument(
        "--h265",
        choices=("yes", "no", "unknown"),
        help="Published or measured H.265 support override.",
    )
    parser.add_argument(
        "--dual-stream",
        choices=("yes", "no", "unknown"),
        help="Published or measured dual-stream support override.",
    )
    parser.add_argument(
        "--triple-stream",
        choices=("yes", "no", "unknown"),
        help="Published or measured triple-stream support override.",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Published spec/source URL. Repeatable.",
    )
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="Additional note. Repeatable.",
    )
    parser.add_argument(
        "--published-only",
        action="store_true",
        help="Skip network probing and emit a datasheet-seeded UNTESTED report.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write the JSON report. Defaults to stdout.",
    )
    args = parser.parse_args()

    if not args.published_only and not args.host and not args.rtsp_url:
        parser.error("live probe requires --host or --rtsp-url")
    if args.host and (not args.username or not args.password):
        parser.error("--host requires --username and --password")
    return args


def redact_rtsp_uri(uri: str | None) -> str | None:
    if not uri:
        return None
    parts = urlsplit(uri)
    if "@" not in parts.netloc:
        return uri
    host_part = parts.netloc.split("@", 1)[1]
    return urlunsplit((parts.scheme, host_part, parts.path, parts.query, parts.fragment))


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def parse_tristate(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "yes":
        return True
    if normalized == "no":
        return False
    return None


def tristate_text(value: bool | None, fallback: str = "Unknown") -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return fallback


def make_capability(
    supported: bool | None,
    summary: str | None,
    **details: Any,
) -> CapabilityReport:
    return CapabilityReport(supported=supported, summary=summary, details=details)


def build_published_report(args: argparse.Namespace) -> CameraReport:
    h265_supported = parse_tristate(args.h265)
    dual_stream_supported = parse_tristate(args.dual_stream)
    triple_stream_supported = parse_tristate(args.triple_stream)

    onvif = make_capability(
        None if not args.onvif_summary else True,
        args.onvif_summary or "Published data only",
    )
    rtsp = make_capability(
        None if not args.rtsp_summary else True,
        args.rtsp_summary or "Published data only",
        probe_uri=redact_rtsp_uri(args.rtsp_url),
    )
    h265 = make_capability(h265_supported, tristate_text(h265_supported))
    dual_stream = make_capability(
        dual_stream_supported, tristate_text(dual_stream_supported)
    )
    triple_stream = make_capability(
        triple_stream_supported, tristate_text(triple_stream_supported)
    )

    report = CameraReport(
        brand=args.brand,
        model=args.model,
        firmware=args.firmware,
        status=args.status or "UNTESTED",
        published_only=True,
        probe_timestamp_utc=utc_now(),
        onvif=onvif,
        rtsp=rtsp,
        h265=h265,
        dual_stream=dual_stream,
        triple_stream=triple_stream,
        ptz=make_capability(None, "Not probed"),
        ir_detect=args.ir_detect,
        ik10=args.ik10,
        smart_codec=args.smart_codec,
        streams=[],
        sources=args.source,
        notes=args.note,
        matrix_columns={},
    )
    report.matrix_columns = build_matrix_columns(report)
    return report


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def probe_onvif(args: argparse.Namespace) -> tuple[CapabilityReport, list[StreamReport], CapabilityReport, str | None, str | None]:
    if not args.host:
        return (
            make_capability(None, "Not probed"),
            [],
            make_capability(None, "Not probed"),
            None,
            None,
        )

    onvif_mod = require_module("onvif", "onvif-zeep")
    ONVIFCamera = getattr(onvif_mod, "ONVIFCamera")

    camera = ONVIFCamera(args.host, args.onvif_port, args.username, args.password)
    device_mgmt = camera.create_devicemgmt_service()
    device_info = device_mgmt.GetDeviceInformation()
    services = device_mgmt.GetServices({"IncludeCapability": True})

    namespaces = [str(getattr(service, "Namespace", "")) for service in services]
    detected_profiles: list[str] = []
    ns_blob = " ".join(namespaces).lower()
    if "media/wsdl" in ns_blob:
        detected_profiles.append("S")
    if "media2/wsdl" in ns_blob or "ver20/media/wsdl" in ns_blob:
        detected_profiles.append("T")
    if any(token in ns_blob for token in ("recording/wsdl", "replay/wsdl", "search/wsdl")):
        detected_profiles.append("G")
    detected_profiles = sorted(set(detected_profiles))

    media = camera.create_media_service()
    profiles = media.GetProfiles()
    streams: list[StreamReport] = []
    ptz_supported = False
    ir_detect = None

    for profile in profiles:
        encoder = getattr(profile, "VideoEncoderConfiguration", None)
        codec = None
        resolution = None
        if encoder is not None:
            codec = str(getattr(encoder, "Encoding", "") or "") or None
            resolution_obj = getattr(encoder, "Resolution", None)
            if resolution_obj is not None:
                width = getattr(resolution_obj, "Width", None)
                height = getattr(resolution_obj, "Height", None)
                if width and height:
                    resolution = f"{width}x{height}"

        try:
            request = media.create_type("GetStreamUri")
            request.ProfileToken = profile.token
            request.StreamSetup = {
                "Stream": "RTP-Unicast",
                "Transport": {"Protocol": "RTSP"},
            }
            stream_uri = media.GetStreamUri(request).Uri
        except Exception:
            stream_uri = None

        if getattr(profile, "PTZConfiguration", None) is not None:
            ptz_supported = True

        streams.append(
            StreamReport(
                name=str(getattr(profile, "Name", "") or f"profile-{profile.token}"),
                token=str(getattr(profile, "token", "")) or None,
                resolution=resolution,
                codec=codec,
                rtsp_uri=redact_rtsp_uri(stream_uri),
            )
        )

    try:
        imaging = camera.create_imaging_service()
        video_source_token = None
        for profile in profiles:
            video_source_cfg = getattr(profile, "VideoSourceConfiguration", None)
            if video_source_cfg is not None:
                video_source_token = getattr(video_source_cfg, "SourceToken", None)
                if video_source_token:
                    break
        if video_source_token:
            settings = imaging.GetImagingSettings({"VideoSourceToken": video_source_token})
            ir_cut = getattr(settings, "IrCutFilter", None)
            if ir_cut:
                ir_detect = f"ONVIF imaging exposes IrCutFilter={ir_cut}"
            else:
                day_night = getattr(settings, "DayNight", None)
                if day_night:
                    ir_detect = f"ONVIF imaging exposes DayNight={day_night}"
    except Exception:
        ir_detect = None

    onvif_summary = "Unavailable"
    if detected_profiles:
        onvif_summary = "/".join(detected_profiles)
    elif profiles:
        onvif_summary = "Media service reachable"

    ptz = make_capability(
        ptz_supported,
        "Yes" if ptz_supported else "No",
        service_namespaces=namespaces,
    )
    firmware = str(getattr(device_info, "FirmwareVersion", "") or "") or None

    onvif = make_capability(
        True,
        onvif_summary,
        profiles=detected_profiles,
        manufacturer=str(getattr(device_info, "Manufacturer", "") or "") or None,
        hardware_id=str(getattr(device_info, "HardwareId", "") or "") or None,
        model=str(getattr(device_info, "Model", "") or "") or None,
        service_namespaces=namespaces,
    )
    return onvif, streams, ptz, firmware, ir_detect


def choose_rtsp_uri(args: argparse.Namespace, streams: list[StreamReport]) -> str | None:
    if args.rtsp_url:
        return args.rtsp_url
    for stream in streams:
        if stream.rtsp_uri:
            return stream.rtsp_uri
    return None


def probe_rtsp(args: argparse.Namespace, uri: str | None) -> CapabilityReport:
    if not uri:
        return make_capability(None, "Not probed")

    cv2 = require_module("cv2", "opencv-python or opencv-python-headless")
    if hasattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC"):
        capture = cv2.VideoCapture()
        capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(args.timeout_s * 1000))
        capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(args.timeout_s * 1000))
        opened = capture.open(uri, cv2.CAP_FFMPEG) if hasattr(cv2, "CAP_FFMPEG") else capture.open(uri)
    else:  # pragma: no cover - OpenCV build dependent
        capture = cv2.VideoCapture(uri, cv2.CAP_FFMPEG) if hasattr(cv2, "CAP_FFMPEG") else cv2.VideoCapture(uri)
        opened = capture.isOpened()

    frame_read = False
    width = None
    height = None
    if opened:
        frame_read, frame = capture.read()
        if frame_read and frame is not None:
            height, width = frame.shape[:2]
    capture.release()

    supported = bool(opened and frame_read)
    summary = "Yes" if supported else "No"
    if supported and width and height:
        summary = f"Yes ({width}x{height} frame read)"
    elif opened and not frame_read:
        summary = "Opened but no frame read"

    return make_capability(
        supported,
        summary,
        probe_uri=redact_rtsp_uri(uri),
        opened=opened,
        frame_read=frame_read,
        width=width,
        height=height,
    )


def derive_stream_capabilities(streams: list[StreamReport]) -> tuple[CapabilityReport, CapabilityReport, CapabilityReport]:
    codecs = {(stream.codec or "").upper() for stream in streams if stream.codec}
    h265_supported = "H265" in codecs or "HEVC" in codecs
    stream_count = len(streams)

    h265 = make_capability(
        h265_supported if streams else None,
        "Yes" if h265_supported else ("No" if streams else "Unknown"),
        codecs=sorted(codecs),
    )
    dual_stream = make_capability(
        stream_count >= 2 if streams else None,
        "Yes" if stream_count >= 2 else ("No" if streams else "Unknown"),
        stream_count=stream_count,
    )
    triple_stream = make_capability(
        stream_count >= 3 if streams else None,
        "Yes" if stream_count >= 3 else ("No" if streams else "Unknown"),
        stream_count=stream_count,
    )
    return h265, dual_stream, triple_stream


def apply_manual_overrides(report: CameraReport, args: argparse.Namespace) -> None:
    if args.firmware:
        report.firmware = args.firmware
    if args.ir_detect:
        report.ir_detect = args.ir_detect
    if args.ik10:
        report.ik10 = args.ik10
    if args.smart_codec:
        report.smart_codec = args.smart_codec
    if args.onvif_summary:
        report.onvif.summary = args.onvif_summary
        if report.onvif.supported is None:
            report.onvif.supported = True
    if args.rtsp_summary:
        report.rtsp.summary = args.rtsp_summary
        if report.rtsp.supported is None:
            report.rtsp.supported = True
    if args.h265:
        report.h265.supported = parse_tristate(args.h265)
        report.h265.summary = tristate_text(report.h265.supported)
    if args.dual_stream:
        report.dual_stream.supported = parse_tristate(args.dual_stream)
        report.dual_stream.summary = tristate_text(report.dual_stream.supported)
    if args.triple_stream:
        report.triple_stream.supported = parse_tristate(args.triple_stream)
        report.triple_stream.summary = tristate_text(report.triple_stream.supported)


def derive_status(args: argparse.Namespace, report: CameraReport, errors: list[str]) -> str:
    if args.status:
        return args.status
    if report.published_only:
        return "UNTESTED"
    if errors:
        return "TESTED-ISSUES"
    if report.rtsp.supported and (report.onvif.supported or report.onvif.supported is None):
        return "TESTED-OK"
    if report.onvif.supported or report.rtsp.supported:
        return "TESTED-ISSUES"
    return "TESTED-ISSUES"


def capability_display(capability: CapabilityReport) -> str:
    if capability.summary:
        return capability.summary
    return tristate_text(capability.supported)


def build_matrix_columns(report: CameraReport) -> dict[str, str]:
    return {
        "Brand": report.brand,
        "Model": report.model,
        "Firmware": report.firmware or "Unverified",
        "ONVIF": capability_display(report.onvif),
        "RTSP": capability_display(report.rtsp),
        "H.265": capability_display(report.h265),
        "Dual Stream": capability_display(report.dual_stream),
        "Triple Stream": capability_display(report.triple_stream),
        "IR Detect": report.ir_detect or "Unknown",
        "IK10": report.ik10 or "Unknown",
        "Smart Codec": report.smart_codec or "Unknown",
        "Status": report.status,
    }


def report_to_json(report: CameraReport) -> str:
    payload = asdict(report)
    return json.dumps(payload, indent=2, sort_keys=True)


def main() -> int:
    args = parse_args()

    if args.published_only:
        report = build_published_report(args)
        output_report(report, args.output)
        return 0

    errors: list[str] = []
    notes = list(args.note)
    onvif = make_capability(None, "Not probed")
    streams: list[StreamReport] = []
    ptz = make_capability(None, "Not probed")
    firmware = args.firmware
    ir_detect = args.ir_detect

    try:
        onvif, streams, ptz, onvif_firmware, onvif_ir_detect = probe_onvif(args)
        firmware = firmware or onvif_firmware
        ir_detect = ir_detect or onvif_ir_detect
    except MissingDependencyError:
        raise
    except Exception as exc:
        errors.append(f"ONVIF probe failed: {exc}")
        notes.append(f"ONVIF probe failed: {exc}")

    rtsp_uri = choose_rtsp_uri(args, streams)
    try:
        rtsp = probe_rtsp(args, rtsp_uri)
    except MissingDependencyError:
        raise
    except Exception as exc:
        errors.append(f"RTSP probe failed: {exc}")
        notes.append(f"RTSP probe failed: {exc}")
        rtsp = make_capability(False if rtsp_uri else None, "Probe failed")

    h265, dual_stream, triple_stream = derive_stream_capabilities(streams)

    report = CameraReport(
        brand=args.brand,
        model=args.model,
        firmware=firmware,
        status="UNSET",
        published_only=False,
        probe_timestamp_utc=utc_now(),
        onvif=onvif,
        rtsp=rtsp,
        h265=h265,
        dual_stream=dual_stream,
        triple_stream=triple_stream,
        ptz=ptz,
        ir_detect=ir_detect,
        ik10=args.ik10,
        smart_codec=args.smart_codec,
        streams=streams,
        sources=args.source,
        notes=notes,
        matrix_columns={},
    )
    apply_manual_overrides(report, args)
    report.status = derive_status(args, report, errors)
    report.matrix_columns = build_matrix_columns(report)
    output_report(report, args.output)
    return 0


def output_report(report: CameraReport, output_path: Path | None) -> None:
    payload = report_to_json(report)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    else:
        sys.stdout.write(payload)
        sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MissingDependencyError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
