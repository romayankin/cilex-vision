"""Triton gRPC client for YOLOv8-L object detection.

Handles preprocessing (letterbox, normalise), Triton inference, and
post-processing (transpose, NMS, confidence filter).

YOLOv8 output shape is ``[batch, 4+num_classes, 8400]`` — transposed
relative to the intuitive ``[batch, 8400, 4+num_classes]`` layout.
NMS is the client's responsibility (not built into the TensorRT engine).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import numpy as np

from config import DetectorConfig, TritonConfig
from metrics import DETECTIONS_TOTAL, INFERENCE_LATENCY

logger = logging.getLogger(__name__)

# Proto enum values → DB/taxonomy lowercase names
CLASS_INDEX_TO_NAME: dict[int, str] = {
    0: "person",
    1: "car",
    2: "truck",
    3: "bus",
    4: "bicycle",
    5: "motorcycle",
    6: "animal",
}

# Proto enum integer values (1-based, matching detection.proto ObjectClass)
CLASS_INDEX_TO_PROTO: dict[int, int] = {
    0: 1,  # OBJECT_CLASS_PERSON
    1: 2,  # OBJECT_CLASS_CAR
    2: 3,  # OBJECT_CLASS_TRUCK
    3: 4,  # OBJECT_CLASS_BUS
    4: 5,  # OBJECT_CLASS_BICYCLE
    5: 6,  # OBJECT_CLASS_MOTORCYCLE
    6: 7,  # OBJECT_CLASS_ANIMAL
}


@dataclass
class RawDetection:
    """A single post-NMS detection in normalised [0,1] coordinates."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float
    confidence: float
    class_index: int  # 0-6, maps via CLASS_INDEX_TO_NAME

    @property
    def class_name(self) -> str:
        return CLASS_INDEX_TO_NAME[self.class_index]

    @property
    def proto_class(self) -> int:
        return CLASS_INDEX_TO_PROTO[self.class_index]


@dataclass
class LetterboxInfo:
    """Stores letterbox parameters for coordinate un-mapping."""

    scale: float
    pad_w: float
    pad_h: float
    orig_w: int
    orig_h: int


class DetectorClient:
    """Triton gRPC client for YOLOv8-L inference + NMS."""

    def __init__(
        self,
        triton_cfg: TritonConfig,
        detector_cfg: DetectorConfig,
    ) -> None:
        self._triton_cfg = triton_cfg
        self._det_cfg = detector_cfg
        self._client = None  # lazy init

    def _get_client(self) -> object:
        if self._client is None:
            import tritonclient.grpc as grpcclient  # noqa: PLC0415

            self._client = grpcclient.InferenceServerClient(
                url=self._triton_cfg.url,
            )
        return self._client

    async def detect(
        self, frame: np.ndarray
    ) -> list[RawDetection]:
        """Run detection on a single RGB frame (H, W, 3) uint8.

        Returns post-NMS detections with normalised bbox coordinates.
        """
        orig_h, orig_w = frame.shape[:2]
        input_tensor, lb_info = self._preprocess(frame)

        t0 = time.monotonic()
        raw_output = await asyncio.to_thread(
            self._infer, input_tensor
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        INFERENCE_LATENCY.observe(elapsed_ms)

        detections = self._postprocess(raw_output, lb_info)
        for det in detections:
            DETECTIONS_TOTAL.labels(object_class=det.class_name).inc()
        return detections

    # ------------------------------------------------------------------
    # Preprocessing
    # ------------------------------------------------------------------

    def _preprocess(
        self, frame: np.ndarray
    ) -> tuple[np.ndarray, LetterboxInfo]:
        """Letterbox resize to 640×640, divide by 255, NCHW layout."""
        sz = self._det_cfg.input_size
        orig_h, orig_w = frame.shape[:2]

        scale = min(sz / orig_w, sz / orig_h)
        new_w = int(round(orig_w * scale))
        new_h = int(round(orig_h * scale))
        pad_w = (sz - new_w) / 2.0
        pad_h = (sz - new_h) / 2.0

        # Resize using numpy bilinear (avoid heavy cv2 dependency)
        resized = _resize_bilinear(frame, new_w, new_h)

        # Pad with gray (114)
        padded = np.full((sz, sz, 3), 114, dtype=np.uint8)
        top = int(round(pad_h))
        left = int(round(pad_w))
        padded[top : top + new_h, left : left + new_w] = resized

        # HWC→CHW, uint8→float32, /255
        tensor = padded.transpose(2, 0, 1).astype(np.float32) / 255.0
        # Add batch dim: (1, 3, 640, 640)
        tensor = np.expand_dims(tensor, axis=0)

        lb_info = LetterboxInfo(
            scale=scale,
            pad_w=pad_w,
            pad_h=pad_h,
            orig_w=orig_w,
            orig_h=orig_h,
        )
        return tensor, lb_info

    # ------------------------------------------------------------------
    # Triton inference
    # ------------------------------------------------------------------

    def _infer(self, input_tensor: np.ndarray) -> np.ndarray:
        """Synchronous Triton gRPC inference (called via to_thread)."""
        import tritonclient.grpc as grpcclient  # noqa: PLC0415

        client = self._get_client()
        inputs = [
            grpcclient.InferInput(
                self._triton_cfg.detector_input_name,
                list(input_tensor.shape),
                "FP32",
            )
        ]
        inputs[0].set_data_from_numpy(input_tensor)
        outputs = [
            grpcclient.InferRequestedOutput(
                self._triton_cfg.detector_output_name,
            )
        ]
        result = client.infer(  # type: ignore[union-attr]
            model_name=self._triton_cfg.detector_model,
            inputs=inputs,
            outputs=outputs,
        )
        return result.as_numpy(self._triton_cfg.detector_output_name)

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------

    def _postprocess(
        self,
        raw: np.ndarray,
        lb_info: LetterboxInfo,
    ) -> list[RawDetection]:
        """Transpose, filter, NMS, un-letterbox."""
        # raw shape: [batch, 11, 8400] → [8400, 11]
        preds = raw[0].T  # (8400, 11)

        cx = preds[:, 0]
        cy = preds[:, 1]
        w = preds[:, 2]
        h = preds[:, 3]
        class_scores = preds[:, 4:]  # (8400, 7)

        # Best class per anchor
        class_ids = np.argmax(class_scores, axis=1)
        confidences = class_scores[np.arange(len(class_ids)), class_ids]

        # Confidence filter
        thresh = self._det_cfg.confidence_threshold
        mask = confidences >= thresh
        if not np.any(mask):
            return []

        cx = cx[mask]
        cy = cy[mask]
        w = w[mask]
        h = h[mask]
        confidences = confidences[mask]
        class_ids = class_ids[mask]

        # Convert center-format to corner-format (pixel coords in 640×640 space)
        x1 = cx - w / 2
        y1 = cy - h / 2
        x2 = cx + w / 2
        y2 = cy + h / 2

        # Per-class NMS
        keep = _per_class_nms(
            x1, y1, x2, y2, confidences, class_ids,
            self._det_cfg.nms_iou_threshold,
        )

        if len(keep) == 0:
            return []

        x1 = x1[keep]
        y1 = y1[keep]
        x2 = x2[keep]
        y2 = y2[keep]
        confidences = confidences[keep]
        class_ids = class_ids[keep]

        # Un-letterbox → normalised [0,1]
        detections: list[RawDetection] = []
        for i in range(len(keep)):
            nx1 = (float(x1[i]) - lb_info.pad_w) / lb_info.scale / lb_info.orig_w
            ny1 = (float(y1[i]) - lb_info.pad_h) / lb_info.scale / lb_info.orig_h
            nx2 = (float(x2[i]) - lb_info.pad_w) / lb_info.scale / lb_info.orig_w
            ny2 = (float(y2[i]) - lb_info.pad_h) / lb_info.scale / lb_info.orig_h

            # Clip to [0, 1]
            nx1 = max(0.0, min(1.0, nx1))
            ny1 = max(0.0, min(1.0, ny1))
            nx2 = max(0.0, min(1.0, nx2))
            ny2 = max(0.0, min(1.0, ny2))

            if nx2 <= nx1 or ny2 <= ny1:
                continue

            detections.append(
                RawDetection(
                    x_min=nx1,
                    y_min=ny1,
                    x_max=nx2,
                    y_max=ny2,
                    confidence=float(confidences[i]),
                    class_index=int(class_ids[i]),
                )
            )
        return detections


# ------------------------------------------------------------------
# Pure-numpy helpers
# ------------------------------------------------------------------


def _resize_bilinear(img: np.ndarray, new_w: int, new_h: int) -> np.ndarray:
    """Simple bilinear resize without OpenCV."""
    old_h, old_w = img.shape[:2]
    if old_h == new_h and old_w == new_w:
        return img

    # Use numpy-based nearest-neighbor for speed in this context.
    # For production, Pillow or cv2 would be used; this avoids the dependency.
    from PIL import Image  # noqa: PLC0415

    pil_img = Image.fromarray(img)
    resized = pil_img.resize((new_w, new_h), Image.BILINEAR)
    return np.array(resized)


def _per_class_nms(
    x1: np.ndarray,
    y1: np.ndarray,
    x2: np.ndarray,
    y2: np.ndarray,
    scores: np.ndarray,
    class_ids: np.ndarray,
    iou_thresh: float,
) -> np.ndarray:
    """Per-class NMS returning indices to keep."""
    keep: list[int] = []
    unique_classes = np.unique(class_ids)
    for cls in unique_classes:
        cls_mask = class_ids == cls
        cls_indices = np.where(cls_mask)[0]
        cls_keep = _nms(
            x1[cls_indices],
            y1[cls_indices],
            x2[cls_indices],
            y2[cls_indices],
            scores[cls_indices],
            iou_thresh,
        )
        keep.extend(cls_indices[cls_keep].tolist())
    return np.array(keep, dtype=np.int64) if keep else np.array([], dtype=np.int64)


def _nms(
    x1: np.ndarray,
    y1: np.ndarray,
    x2: np.ndarray,
    y2: np.ndarray,
    scores: np.ndarray,
    iou_thresh: float,
) -> list[int]:
    """Standard greedy NMS on a single class."""
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []

    while len(order) > 0:
        i = order[0]
        keep.append(int(i))
        if len(order) == 1:
            break

        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[i] + areas[order[1:]] - inter
        iou = inter / np.maximum(union, 1e-8)

        remaining = np.where(iou <= iou_thresh)[0]
        order = order[remaining + 1]

    return keep
