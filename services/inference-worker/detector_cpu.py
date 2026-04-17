"""CPU-only YOLOv8s detector using ultralytics.

Drop-in replacement for DetectorClient (Triton gRPC) when Triton/GPU
is not available. Outputs RawDetection objects with the same normalised
[0,1] bbox layout so downstream code (tracker, publisher) is unchanged.

YOLOv8s is the 11.2M-param "small" model — 44.9% mAP on COCO, ~70ms/frame
on i5-13500 CPU. A base confidence of 0.40 is used to pull in more
candidates, with class-specific thresholds applied in post-processing
(e.g., animal=0.85 to filter false positives, person=0.50 for recall).
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from detector_client import (
    CLASS_INDEX_TO_NAME,
    CLASS_INDEX_TO_PROTO,
    RawDetection,
)
from metrics import DETECTIONS_TOTAL, INFERENCE_LATENCY

logger = logging.getLogger(__name__)

# COCO class indices (ultralytics YOLOv8 default) → Cilex class indices.
# Restricted to person and car only for the current pilot.
# Truck/bus/bicycle/motorcycle/animal are intentionally not mapped —
# their CLASS_INDEX_TO_NAME entries remain in detector_client.py so
# historical records from before this restriction still render correctly.
COCO_TO_CILEX: dict[int, int] = {
    0: 0,   # person
    2: 1,   # car
}

# Default class-specific confidence thresholds (Cilex class indices).
DEFAULT_CLASS_THRESHOLDS: dict[int, float] = {
    0: 0.50,   # person
    1: 0.50,   # car
}

# Cilex class name → index (inverse of CLASS_INDEX_TO_NAME, used when
# converting config dict-of-names to dict-of-indices).
CILEX_NAME_TO_INDEX: dict[str, int] = {
    "person": 0,
    "car": 1,
    "truck": 2,
    "bus": 3,
    "bicycle": 4,
    "motorcycle": 5,
    "animal": 6,
}


class CpuDetectorClient:
    """Ultralytics YOLOv8s CPU detector with the DetectorClient interface."""

    def __init__(
        self,
        confidence_threshold: float = 0.40,
        nms_iou_threshold: float = 0.45,
        model_name: str = "yolov8s.pt",
        class_thresholds: dict[str, float] | None = None,
    ) -> None:
        self._confidence = confidence_threshold
        self._iou = nms_iou_threshold
        self._model_name = model_name
        self._model = None  # lazy init

        # Resolve class-name keyed thresholds into class-index keyed dict.
        resolved: dict[int, float] = dict(DEFAULT_CLASS_THRESHOLDS)
        if class_thresholds:
            for name, thr in class_thresholds.items():
                idx = CILEX_NAME_TO_INDEX.get(name)
                if idx is not None:
                    resolved[idx] = float(thr)
        self._class_thresholds = resolved

    def _get_model(self) -> object:
        if self._model is None:
            from ultralytics import YOLO  # noqa: PLC0415

            logger.info("Loading %s on CPU", self._model_name)
            self._model = YOLO(self._model_name)
        return self._model

    async def detect(self, frame: np.ndarray) -> list[RawDetection]:
        """Run YOLOv8s CPU inference on a single RGB frame (H, W, 3) uint8."""
        t0 = time.monotonic()
        results = await asyncio.to_thread(self._infer, frame)
        elapsed_ms = (time.monotonic() - t0) * 1000
        INFERENCE_LATENCY.observe(elapsed_ms)

        detections = self._postprocess(results, frame.shape[0], frame.shape[1])
        for det in detections:
            DETECTIONS_TOTAL.labels(object_class=det.class_name).inc()
        return detections

    def _infer(self, frame: np.ndarray) -> object:
        model = self._get_model()
        return model.predict(
            frame,
            conf=self._confidence,
            iou=self._iou,
            device="cpu",
            verbose=False,
        )

    def _postprocess(
        self, results: object, orig_h: int, orig_w: int
    ) -> list[RawDetection]:
        detections: list[RawDetection] = []
        if not results:
            return detections

        result = results[0]
        boxes = getattr(result, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return detections

        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        clses = boxes.cls.cpu().numpy().astype(int)

        for i in range(len(xyxy)):
            coco_cls = int(clses[i])
            if coco_cls not in COCO_TO_CILEX:
                continue
            cilex_cls = COCO_TO_CILEX[coco_cls]
            if cilex_cls not in CLASS_INDEX_TO_NAME:
                continue

            # Apply class-specific confidence threshold. The base conf=0.40
            # passed to YOLO is a floor; each class then has its own gate.
            class_threshold = self._class_thresholds.get(cilex_cls, self._confidence)
            if float(confs[i]) < class_threshold:
                continue

            x1, y1, x2, y2 = (float(v) for v in xyxy[i])
            nx1 = max(0.0, min(1.0, x1 / orig_w))
            ny1 = max(0.0, min(1.0, y1 / orig_h))
            nx2 = max(0.0, min(1.0, x2 / orig_w))
            ny2 = max(0.0, min(1.0, y2 / orig_h))
            if nx2 <= nx1 or ny2 <= ny1:
                continue

            detections.append(
                RawDetection(
                    x_min=nx1,
                    y_min=ny1,
                    x_max=nx2,
                    y_max=ny2,
                    confidence=float(confs[i]),
                    class_index=cilex_cls,
                )
            )
        return detections


# Ensure the proto mapping covers all Cilex class indices declared above.
assert all(c in CLASS_INDEX_TO_PROTO for c in CLASS_INDEX_TO_NAME)
