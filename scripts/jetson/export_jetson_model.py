"""Export YOLOv8n to Jetson-compatible TensorRT INT8 engine.

Usage:
    python export_jetson_model.py --model yolov8n.pt \\
        --output models/jetson/yolov8n-int8.engine \\
        --imgsz 640 --int8 --device jetson

Supports two export paths:
  1. PyTorch → ONNX → TensorRT (on host or Jetson)
  2. ONNX → TensorRT (if .onnx model provided)

INT8 calibration requires a calibration dataset directory with
representative images. FP16 is always enabled as a fallback precision.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 7 classes matching proto ObjectClass
CLASS_NAMES = ("person", "car", "truck", "bus", "bicycle", "motorcycle", "animal")


def export_to_onnx(model_path: str, output_path: str, imgsz: int) -> str:
    """Export PyTorch YOLOv8 model to ONNX format."""
    try:
        from ultralytics import YOLO  # noqa: PLC0415
    except ImportError:
        logger.error("ultralytics package required for PyTorch export: pip install ultralytics")
        sys.exit(1)

    model = YOLO(model_path)
    onnx_path = model.export(format="onnx", imgsz=imgsz, simplify=True, opset=17)
    logger.info("ONNX export complete: %s", onnx_path)

    if output_path and onnx_path != output_path:
        os.rename(onnx_path, output_path)
        return output_path
    return str(onnx_path)


def build_trt_engine(
    onnx_path: str,
    output_path: str,
    imgsz: int,
    fp16: bool,
    int8: bool,
    calibration_data: str | None,
) -> None:
    """Build a TensorRT engine from an ONNX model."""
    try:
        import tensorrt as trt  # noqa: PLC0415
    except ImportError:
        logger.error("tensorrt package required: install via JetPack or pip install tensorrt")
        sys.exit(1)

    trt_logger = trt.Logger(trt.Logger.INFO)
    builder = trt.Builder(trt_logger)
    network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
    parser = trt.OnnxParser(network, trt_logger)

    with open(onnx_path, "rb") as f:
        if not parser.parse(f.read()):
            for i in range(parser.num_errors):
                logger.error("ONNX parse error: %s", parser.get_error(i))
            sys.exit(1)

    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)  # 1 GB

    if fp16 or int8:
        config.set_flag(trt.BuilderFlag.FP16)
        logger.info("FP16 precision enabled")

    if int8:
        config.set_flag(trt.BuilderFlag.INT8)
        if calibration_data:
            calibrator = _build_calibrator(calibration_data, imgsz)
            config.int8_calibrator = calibrator
            logger.info("INT8 calibration with data from: %s", calibration_data)
        else:
            logger.warning(
                "INT8 requested without calibration data -- engine will use "
                "FP16 fallback for uncalibrated layers"
            )

    logger.info("Building TensorRT engine (this may take several minutes)...")
    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        logger.error("TensorRT engine build failed")
        sys.exit(1)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(serialized)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info("TensorRT engine saved: %s (%.1f MB)", output_path, size_mb)

    _print_model_stats(output_path, imgsz, fp16, int8)


def _build_calibrator(data_dir: str, imgsz: int) -> object:
    """Build an INT8 entropy calibrator from a directory of images."""
    import tensorrt as trt  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    class ImageCalibrator(trt.IInt8EntropyCalibrator2):
        def __init__(self, data_dir: str, imgsz: int, batch_size: int = 8) -> None:
            super().__init__()
            self._data_dir = data_dir
            self._imgsz = imgsz
            self._batch_size = batch_size
            self._files = sorted(
                p
                for p in Path(data_dir).iterdir()
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp")
            )
            self._idx = 0
            self._cache_file = os.path.join(data_dir, "calibration.cache")

            try:
                import pycuda.autoinit  # noqa: F401, PLC0415
                import pycuda.driver as cuda  # noqa: PLC0415

                self._d_input = cuda.mem_alloc(
                    batch_size * 3 * imgsz * imgsz * 4  # float32
                )
            except ImportError:
                self._d_input = None

        def get_batch_size(self) -> int:
            return self._batch_size

        def get_batch(self, names: list[str], p_str: str | None = None) -> list[int] | None:
            if self._idx >= len(self._files) or self._d_input is None:
                return None

            import pycuda.driver as cuda  # noqa: PLC0415
            from PIL import Image  # noqa: PLC0415

            batch = []
            for _ in range(self._batch_size):
                if self._idx >= len(self._files):
                    break
                img = Image.open(self._files[self._idx]).convert("RGB")
                img = img.resize((self._imgsz, self._imgsz))
                arr = np.array(img, dtype=np.float32) / 255.0
                arr = arr.transpose(2, 0, 1)  # HWC → CHW
                batch.append(arr)
                self._idx += 1

            if not batch:
                return None

            batch_arr = np.zeros(
                (self._batch_size, 3, self._imgsz, self._imgsz), dtype=np.float32
            )
            for i, b in enumerate(batch):
                batch_arr[i] = b

            cuda.memcpy_htod(self._d_input, batch_arr)
            return [int(self._d_input)]

        def read_calibration_cache(self) -> bytes | None:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, "rb") as f:
                    return f.read()
            return None

        def write_calibration_cache(self, cache: bytes) -> None:
            with open(self._cache_file, "wb") as f:
                f.write(cache)

    return ImageCalibrator(data_dir, imgsz)


def _print_model_stats(
    engine_path: str, imgsz: int, fp16: bool, int8: bool
) -> None:
    """Print model statistics after build."""
    size_mb = os.path.getsize(engine_path) / (1024 * 1024)
    precision = "INT8" if int8 else ("FP16" if fp16 else "FP32")
    print("\n=== Model Export Summary ===")
    print(f"  Engine:     {engine_path}")
    print(f"  Size:       {size_mb:.1f} MB")
    print(f"  Input:      1x3x{imgsz}x{imgsz}")
    print(f"  Precision:  {precision}")
    print(f"  Classes:    {len(CLASS_NAMES)} ({', '.join(CLASS_NAMES)})")
    print("  Target:     Jetson Orin NX 16GB / AGX Orin")

    if int8:
        print("  Expected:   ~2-4ms inference on Orin NX (MAXN)")
    elif fp16:
        print("  Expected:   ~4-8ms inference on Orin NX (MAXN)")
    print("============================\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export YOLOv8n to TensorRT engine for Jetson deployment"
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Path to YOLOv8n weights (.pt or .onnx)",
    )
    parser.add_argument(
        "--output",
        default="models/jetson/yolov8n-int8.engine",
        help="Output engine path (default: models/jetson/yolov8n-int8.engine)",
    )
    parser.add_argument(
        "--imgsz",
        type=int,
        default=640,
        help="Input image size (default: 640)",
    )
    parser.add_argument(
        "--int8",
        action="store_true",
        help="Enable INT8 quantization",
    )
    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Enable FP16 precision (always on if --int8)",
    )
    parser.add_argument(
        "--calibration-data",
        default=None,
        help="Directory of calibration images for INT8",
    )
    parser.add_argument(
        "--device",
        default="jetson",
        choices=["jetson", "gpu"],
        help="Target device (default: jetson)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    model_path = args.model
    onnx_path = model_path

    # Step 1: export to ONNX if PyTorch weights provided
    if model_path.endswith(".pt"):
        onnx_path = model_path.replace(".pt", ".onnx")
        logger.info("Exporting PyTorch model to ONNX: %s -> %s", model_path, onnx_path)
        onnx_path = export_to_onnx(model_path, onnx_path, args.imgsz)

    # Step 2: build TensorRT engine
    logger.info("Building TensorRT engine from ONNX: %s", onnx_path)
    build_trt_engine(
        onnx_path=onnx_path,
        output_path=args.output,
        imgsz=args.imgsz,
        fp16=args.fp16 or args.int8,  # FP16 always on with INT8
        int8=args.int8,
        calibration_data=args.calibration_data,
    )


if __name__ == "__main__":
    main()
