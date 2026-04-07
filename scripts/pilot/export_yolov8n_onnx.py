#!/usr/bin/env python3
"""Export YOLOv8n to ONNX for Triton CPU inference.

Downloads the ultralytics YOLOv8n pretrained weights and exports to
ONNX format with dynamic batch dimension and 640x640 input.

Output: infra/triton/model-repo/yolov8n/1/model.onnx

Requirements:
    pip install ultralytics onnx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "infra" / "triton" / "model-repo" / "yolov8n" / "1" / "model.onnx"


def export(output_path: Path, imgsz: int = 640) -> None:
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ERROR: ultralytics not installed. Run: pip install ultralytics", file=sys.stderr)
        sys.exit(1)

    print("Downloading YOLOv8n weights...")
    model = YOLO("yolov8n.pt")

    print(f"Exporting to ONNX (input {imgsz}x{imgsz}, dynamic batch)...")
    model.export(
        format="onnx",
        imgsz=imgsz,
        dynamic=True,
        simplify=True,
        opset=17,
    )

    # ultralytics exports next to the .pt file
    exported = Path("yolov8n.onnx")
    if not exported.exists():
        # Check in the model's directory
        for candidate in [Path("runs") / "detect" / "yolov8n.onnx", Path("yolov8n.onnx")]:
            if candidate.exists():
                exported = candidate
                break

    if not exported.exists():
        print("ERROR: ONNX export did not produce expected file", file=sys.stderr)
        sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    exported.rename(output_path)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"Exported: {output_path} ({size_mb:.1f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output ONNX path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--imgsz",
        type=int,
        default=640,
        help="Input image size (default: 640)",
    )
    args = parser.parse_args()

    if args.output.exists():
        print(f"Model already exists: {args.output}")
        print("Delete it first to re-export.")
        return

    export(args.output, args.imgsz)


if __name__ == "__main__":
    main()
