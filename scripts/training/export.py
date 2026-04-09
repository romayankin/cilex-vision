#!/usr/bin/env python3
"""Export trained model: PyTorch checkpoint to ONNX and optionally TensorRT.

Converts a trained detector checkpoint to ONNX format, validates it
with onnxruntime (if available), and optionally converts to TensorRT
engine via trtexec (if available).

Output:
    - model.onnx — ONNX graph
    - model.plan — TensorRT engine (if trtexec available)
    - export_metadata.json — export parameters and validation results

Usage:
    python export.py --checkpoint models/best.pt --output-dir models/exported
    python export.py --checkpoint models/best.pt --opset 17 --precision fp16
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ONNX export
# ---------------------------------------------------------------------------


def export_to_onnx(
    checkpoint_path: str,
    output_path: Path,
    *,
    opset: int = 17,
    input_size: int = 640,
    num_classes: int = 7,
) -> dict[str, Any]:
    """Export a PyTorch model checkpoint to ONNX.

    In production, this would:
    1. Load the PyTorch model from checkpoint
    2. Create dummy input tensor (1, 3, input_size, input_size)
    3. Call torch.onnx.export() with the specified opset version
    4. Return export metadata

    The skeleton documents the interface and writes a placeholder ONNX
    metadata file. Replace the body with actual torch.onnx.export() call.
    """
    log.info("exporting to ONNX: %s -> %s (opset=%d)", checkpoint_path, output_path, opset)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Placeholder: in production, replace with actual ONNX export
    # import torch
    # model = load_model(checkpoint_path)
    # dummy_input = torch.randn(1, 3, input_size, input_size)
    # torch.onnx.export(
    #     model, dummy_input, str(output_path),
    #     opset_version=opset,
    #     input_names=["images"],
    #     output_names=["detections"],
    #     dynamic_axes={"images": {0: "batch"}, "detections": {0: "batch"}},
    # )

    metadata = {
        "source_checkpoint": checkpoint_path,
        "onnx_path": str(output_path),
        "opset_version": opset,
        "input_shape": [1, 3, input_size, input_size],
        "input_names": ["images"],
        "output_names": ["detections"],
        "dynamic_axes": {"images": {"0": "batch"}, "detections": {"0": "batch"}},
        "num_classes": num_classes,
    }

    return metadata


def validate_onnx(onnx_path: Path) -> dict[str, Any]:
    """Validate an ONNX model with onnxruntime.

    Checks:
    1. Model loads without errors
    2. Input/output shapes match expectations
    3. Inference runs on a dummy input

    Returns validation results dict.
    """
    validation: dict[str, Any] = {"valid": False, "errors": []}

    try:
        import onnx
        model = onnx.load(str(onnx_path))
        onnx.checker.check_model(model)
        validation["onnx_check"] = "passed"
        log.info("ONNX model check passed")
    except ImportError:
        validation["onnx_check"] = "skipped (onnx not installed)"
        log.warning("onnx not installed, skipping model check")
    except Exception as exc:
        validation["onnx_check"] = f"failed: {exc}"
        validation["errors"].append(str(exc))
        return validation

    try:
        import onnxruntime as ort
        session = ort.InferenceSession(str(onnx_path))
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        validation["input_names"] = [i.name for i in inputs]
        validation["input_shapes"] = [i.shape for i in inputs]
        validation["output_names"] = [o.name for o in outputs]
        validation["output_shapes"] = [o.shape for o in outputs]
        validation["onnxruntime_check"] = "passed"
        log.info("onnxruntime validation passed")
    except ImportError:
        validation["onnxruntime_check"] = "skipped (onnxruntime not installed)"
        log.warning("onnxruntime not installed, skipping inference check")
    except Exception as exc:
        validation["onnxruntime_check"] = f"failed: {exc}"
        validation["errors"].append(str(exc))

    validation["valid"] = len(validation["errors"]) == 0
    return validation


# ---------------------------------------------------------------------------
# TensorRT export
# ---------------------------------------------------------------------------


def has_trtexec() -> bool:
    """Check if trtexec is available on PATH."""
    return shutil.which("trtexec") is not None


def export_to_tensorrt(
    onnx_path: Path,
    output_path: Path,
    *,
    precision: str = "fp16",
    workspace_mb: int = 4096,
) -> dict[str, Any]:
    """Convert ONNX model to TensorRT engine via trtexec.

    Requires trtexec on PATH (typically from TensorRT installation or
    the Triton container).
    """
    log.info("converting to TensorRT: %s -> %s (precision=%s)", onnx_path, output_path, precision)

    if not has_trtexec():
        log.warning("trtexec not found on PATH; skipping TensorRT conversion")
        return {
            "status": "skipped",
            "reason": "trtexec not on PATH",
            "manual_command": (
                f"trtexec --onnx={onnx_path} --saveEngine={output_path} "
                f"--{precision} --workspace={workspace_mb}"
            ),
        }

    cmd = [
        "trtexec",
        f"--onnx={onnx_path}",
        f"--saveEngine={output_path}",
        f"--workspace={workspace_mb}",
    ]
    if precision == "fp16":
        cmd.append("--fp16")
    elif precision == "int8":
        cmd.append("--int8")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        log.error("trtexec failed: %s", result.stderr)
        return {
            "status": "failed",
            "returncode": result.returncode,
            "stderr": result.stderr[-500:] if result.stderr else "",
        }

    return {
        "status": "success",
        "engine_path": str(output_path),
        "precision": precision,
        "workspace_mb": workspace_mb,
    }


# ---------------------------------------------------------------------------
# Main export logic
# ---------------------------------------------------------------------------


def run_export(args: argparse.Namespace) -> dict[str, Any]:
    """Run the full export pipeline: PyTorch -> ONNX -> TensorRT."""
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    onnx_path = output_dir / "model.onnx"
    trt_path = output_dir / "model.plan"

    # Step 1: ONNX export
    onnx_meta = export_to_onnx(
        args.checkpoint,
        onnx_path,
        opset=args.opset,
        input_size=args.input_size,
        num_classes=args.num_classes,
    )

    # Step 2: ONNX validation
    onnx_validation: dict[str, Any] = {"skipped": True}
    if onnx_path.exists():
        onnx_validation = validate_onnx(onnx_path)
    else:
        log.info("ONNX file not found (skeleton mode); skipping validation")

    # Step 3: TensorRT conversion
    trt_meta: dict[str, Any] = {"status": "skipped", "reason": "ONNX not available"}
    if onnx_path.exists():
        trt_meta = export_to_tensorrt(
            onnx_path,
            trt_path,
            precision=args.precision,
            workspace_mb=args.workspace_mb,
        )

    # Write export metadata
    metadata = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_checkpoint": args.checkpoint,
        "onnx": onnx_meta,
        "onnx_validation": onnx_validation,
        "tensorrt": trt_meta,
    }

    meta_path = output_dir / "export_metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    log.info("export metadata: %s", meta_path)

    return metadata


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkpoint",
        default="models/checkpoints/best.pt",
        help="Path to the trained model checkpoint.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("models/exported"),
        help="Output directory for exported models.",
    )
    parser.add_argument(
        "--opset",
        type=int,
        default=17,
        help="ONNX opset version (default: 17).",
    )
    parser.add_argument(
        "--precision",
        choices=["fp32", "fp16", "int8"],
        default="fp16",
        help="TensorRT precision (default: fp16).",
    )
    parser.add_argument(
        "--workspace-mb",
        type=int,
        default=4096,
        help="TensorRT workspace size in MB (default: 4096).",
    )
    parser.add_argument(
        "--input-size",
        type=int,
        default=640,
        help="Model input size (default: 640).",
    )
    parser.add_argument(
        "--num-classes",
        type=int,
        default=7,
        help="Number of object classes (default: 7).",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()

    metadata = run_export(args)
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
