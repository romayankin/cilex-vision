"""TensorRT inference for YOLOv8n on Jetson (INT8 quantized).

Loads a serialized TensorRT engine and runs detection inference
using the TensorRT Python API (tensorrt + pycuda).

Lazy imports for tensorrt and pycuda so tests can run without a GPU.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# 7 classes matching proto ObjectClass (index 0..6 → person..animal)
CLASS_NAMES = ("person", "car", "truck", "bus", "bicycle", "motorcycle", "animal")
NUM_CLASSES = len(CLASS_NAMES)


@dataclass(frozen=True)
class EdgeDetection:
    """Single detection result from on-device inference."""

    class_id: int  # 0-based index into CLASS_NAMES
    class_name: str
    confidence: float
    x_min: float  # normalised [0, 1]
    y_min: float
    x_max: float
    y_max: float


@dataclass
class InferenceStats:
    """Running statistics for monitoring."""

    total_inferences: int = 0
    total_detections: int = 0
    last_latency_ms: float = 0.0
    thermal_warnings: int = 0


class JetsonDetector:
    """TensorRT-based YOLOv8n detector for Jetson devices."""

    def __init__(
        self,
        engine_path: str,
        input_size: tuple[int, int] = (640, 640),
        confidence_threshold: float = 0.40,
        nms_iou_threshold: float = 0.45,
        max_detections: int = 100,
        thermal_throttle_warn_ms: float = 100.0,
    ) -> None:
        self._engine_path = engine_path
        self._input_h, self._input_w = input_size
        self._conf_threshold = confidence_threshold
        self._nms_iou_threshold = nms_iou_threshold
        self._max_detections = max_detections
        self._thermal_warn_ms = thermal_throttle_warn_ms

        self._context = None  # TRT execution context
        self._stream = None  # CUDA stream
        self._d_input = None  # device input buffer
        self._d_output = None  # device output buffer
        self._h_input: np.ndarray | None = None
        self._h_output: np.ndarray | None = None
        self.stats = InferenceStats()

    def load(self) -> None:
        """Load TensorRT engine and allocate CUDA buffers."""
        import pycuda.autoinit  # noqa: F401, PLC0415
        import pycuda.driver as cuda  # noqa: PLC0415
        import tensorrt as trt  # noqa: PLC0415

        trt_logger = trt.Logger(trt.Logger.WARNING)
        with open(self._engine_path, "rb") as f:
            runtime = trt.Runtime(trt_logger)
            engine = runtime.deserialize_cuda_engine(f.read())

        self._context = engine.create_execution_context()

        # Input: NCHW float32 — batch=1, channels=3, H, W
        input_size = 1 * 3 * self._input_h * self._input_w
        self._h_input = np.empty(input_size, dtype=np.float32)
        self._d_input = cuda.mem_alloc(self._h_input.nbytes)

        # Output: YOLOv8 raw output — [1, 4+NUM_CLASSES, 8400]
        output_size = 1 * (4 + NUM_CLASSES) * 8400
        self._h_output = np.empty(output_size, dtype=np.float32)
        self._d_output = cuda.mem_alloc(self._h_output.nbytes)

        self._stream = cuda.Stream()
        logger.info(
            "TensorRT engine loaded: %s (%dx%d)",
            self._engine_path,
            self._input_w,
            self._input_h,
        )

    def detect(self, frame: np.ndarray) -> list[EdgeDetection]:
        """Run inference on an RGB frame and return filtered detections.

        Parameters
        ----------
        frame:
            RGB uint8 array, shape (H, W, 3). Will be letterbox-resized.

        Returns
        -------
        List of EdgeDetection, sorted by confidence descending.
        """
        import pycuda.driver as cuda  # noqa: PLC0415

        orig_h, orig_w = frame.shape[:2]

        # Preprocess
        input_tensor, scale, pad_x, pad_y = self._preprocess(frame)
        np.copyto(self._h_input, input_tensor.ravel())

        # Inference
        t0 = time.monotonic()
        cuda.memcpy_htod_async(self._d_input, self._h_input, self._stream)
        self._context.execute_async_v2(
            bindings=[int(self._d_input), int(self._d_output)],
            stream_handle=self._stream.handle,
        )
        cuda.memcpy_dtoh_async(self._h_output, self._d_output, self._stream)
        self._stream.synchronize()
        latency_ms = (time.monotonic() - t0) * 1000

        self.stats.total_inferences += 1
        self.stats.last_latency_ms = latency_ms

        if latency_ms > self._thermal_warn_ms:
            self.stats.thermal_warnings += 1
            logger.warning(
                "Inference latency %.1fms exceeds threshold %.1fms — possible thermal throttling",
                latency_ms,
                self._thermal_warn_ms,
            )

        # Postprocess
        raw_output = self._h_output.reshape(1, 4 + NUM_CLASSES, 8400)
        detections = self._postprocess(raw_output[0], scale, pad_x, pad_y, orig_w, orig_h)
        self.stats.total_detections += len(detections)
        return detections

    def _preprocess(
        self, frame: np.ndarray
    ) -> tuple[np.ndarray, float, float, float]:
        """Letterbox resize + normalise + HWC→CHW.

        Returns (tensor, scale, pad_x, pad_y).
        """
        orig_h, orig_w = frame.shape[:2]
        scale = min(self._input_w / orig_w, self._input_h / orig_h)
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        pad_x = (self._input_w - new_w) / 2.0
        pad_y = (self._input_h - new_h) / 2.0

        # Resize using nearest-neighbour (fast, no cv2 dependency)
        resized = _resize_nearest(frame, new_w, new_h)

        # Letterbox: place resized image on gray canvas
        canvas = np.full(
            (self._input_h, self._input_w, 3), 114, dtype=np.uint8
        )
        top = int(pad_y)
        left = int(pad_x)
        canvas[top : top + new_h, left : left + new_w] = resized

        # Normalise and transpose: HWC → CHW, float32 [0, 1]
        tensor = canvas.astype(np.float32) / 255.0
        tensor = tensor.transpose(2, 0, 1)  # CHW
        return tensor, scale, pad_x, pad_y

    def _postprocess(
        self,
        raw: np.ndarray,
        scale: float,
        pad_x: float,
        pad_y: float,
        orig_w: int,
        orig_h: int,
    ) -> list[EdgeDetection]:
        """Confidence filter + NMS on raw [4+NUM_CLASSES, 8400] output."""
        # Transpose to [8400, 4+NUM_CLASSES]
        preds = raw.T  # (8400, 11)

        # Extract boxes (cx, cy, w, h) and class scores
        cx = preds[:, 0]
        cy = preds[:, 1]
        w = preds[:, 2]
        h = preds[:, 3]
        class_scores = preds[:, 4:]  # (8400, NUM_CLASSES)

        # Best class per anchor
        class_ids = np.argmax(class_scores, axis=1)
        confidences = class_scores[np.arange(len(class_ids)), class_ids]

        # Confidence filter
        mask = confidences >= self._conf_threshold
        cx, cy, w, h = cx[mask], cy[mask], w[mask], h[mask]
        class_ids = class_ids[mask]
        confidences = confidences[mask]

        if len(confidences) == 0:
            return []

        # Convert cx,cy,w,h → x1,y1,x2,y2 (in input-image coords)
        x1 = cx - w / 2
        y1 = cy - h / 2
        x2 = cx + w / 2
        y2 = cy + h / 2

        # Remove letterbox padding and rescale to original image
        x1 = (x1 - pad_x) / scale
        y1 = (y1 - pad_y) / scale
        x2 = (x2 - pad_x) / scale
        y2 = (y2 - pad_y) / scale

        # Clip to image bounds
        x1 = np.clip(x1, 0, orig_w)
        y1 = np.clip(y1, 0, orig_h)
        x2 = np.clip(x2, 0, orig_w)
        y2 = np.clip(y2, 0, orig_h)

        # Per-class greedy NMS
        keep = _nms_per_class(
            x1, y1, x2, y2, confidences, class_ids, self._nms_iou_threshold
        )

        # Build results (normalised coords)
        detections: list[EdgeDetection] = []
        for i in keep[: self._max_detections]:
            detections.append(
                EdgeDetection(
                    class_id=int(class_ids[i]),
                    class_name=CLASS_NAMES[class_ids[i]],
                    confidence=float(confidences[i]),
                    x_min=float(x1[i] / orig_w),
                    y_min=float(y1[i] / orig_h),
                    x_max=float(x2[i] / orig_w),
                    y_max=float(y2[i] / orig_h),
                )
            )
        return detections


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _resize_nearest(img: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
    """Nearest-neighbour resize without OpenCV dependency."""
    orig_h, orig_w = img.shape[:2]
    row_indices = (np.arange(new_h) * orig_h / new_h).astype(int)
    col_indices = (np.arange(new_w) * orig_w / new_w).astype(int)
    return img[np.ix_(row_indices, col_indices)]


def _nms_per_class(
    x1: np.ndarray,
    y1: np.ndarray,
    x2: np.ndarray,
    y2: np.ndarray,
    scores: np.ndarray,
    class_ids: np.ndarray,
    iou_threshold: float,
) -> list[int]:
    """Per-class greedy NMS. Returns indices to keep, sorted by confidence."""
    keep: list[int] = []
    for cls in np.unique(class_ids):
        cls_mask = class_ids == cls
        cls_indices = np.where(cls_mask)[0]
        cls_scores = scores[cls_indices]
        order = cls_indices[np.argsort(-cls_scores)]

        while len(order) > 0:
            i = order[0]
            keep.append(int(i))
            if len(order) == 1:
                break

            rest = order[1:]
            inter_x1 = np.maximum(x1[i], x1[rest])
            inter_y1 = np.maximum(y1[i], y1[rest])
            inter_x2 = np.minimum(x2[i], x2[rest])
            inter_y2 = np.minimum(y2[i], y2[rest])
            inter_area = np.maximum(0, inter_x2 - inter_x1) * np.maximum(
                0, inter_y2 - inter_y1
            )
            area_i = (x2[i] - x1[i]) * (y2[i] - y1[i])
            area_rest = (x2[rest] - x1[rest]) * (y2[rest] - y1[rest])
            iou = inter_area / (area_i + area_rest - inter_area + 1e-6)
            order = rest[iou <= iou_threshold]

    # Sort final keeps by confidence descending
    keep.sort(key=lambda idx: -scores[idx])
    return keep
