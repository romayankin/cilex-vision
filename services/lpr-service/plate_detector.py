"""Triton gRPC client for plate detection."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from metrics import DETECTION_LATENCY_MS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlateDetection:
    """Plate bbox relative to the vehicle crop, normalized to [0, 1]."""

    x: float
    y: float
    w: float
    h: float
    confidence: float


class PlateDetectorClient:
    """Two-dimensional detector client for vehicle-localized plates."""

    def __init__(
        self,
        *,
        triton_url: str,
        model_name: str,
        input_name: str,
        output_name: str,
        input_size: int = 640,
        confidence_threshold: float = 0.35,
        nms_iou_threshold: float = 0.40,
    ) -> None:
        self._url = triton_url
        self._model = model_name
        self._input_name = input_name
        self._output_name = output_name
        self._input_size = input_size
        self._confidence_threshold = confidence_threshold
        self._nms_iou_threshold = nms_iou_threshold
        self._client: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self) -> Any:
        if self._client is None:
            import tritonclient.grpc as grpcclient  # noqa: PLC0415

            self._client = grpcclient.InferenceServerClient(url=self._url)
        return self._client

    async def detect(self, vehicle_crop_rgb: np.ndarray) -> list[PlateDetection]:
        """Run plate detection on a vehicle crop."""
        input_tensor = self._preprocess(vehicle_crop_rgb)
        started = time.monotonic()
        raw = await asyncio.to_thread(self._infer, input_tensor)
        DETECTION_LATENCY_MS.observe((time.monotonic() - started) * 1000.0)
        return self._postprocess(raw)

    def _preprocess(self, vehicle_crop_rgb: np.ndarray) -> np.ndarray:
        """Resize to the detector input size and convert to NCHW FP32."""
        image = Image.fromarray(vehicle_crop_rgb)
        resized = image.resize((self._input_size, self._input_size), Image.BILINEAR)
        array = np.asarray(resized, dtype=np.float32) / 255.0
        tensor = array.transpose(2, 0, 1)
        return np.expand_dims(tensor, axis=0)

    def _infer(self, input_tensor: np.ndarray) -> np.ndarray:
        """Synchronous Triton inference, wrapped by asyncio.to_thread."""
        import tritonclient.grpc as grpcclient  # noqa: PLC0415

        client = self._get_client()
        inputs = [
            grpcclient.InferInput(
                self._input_name,
                list(input_tensor.shape),
                "FP32",
            )
        ]
        inputs[0].set_data_from_numpy(input_tensor)
        outputs = [grpcclient.InferRequestedOutput(self._output_name)]
        result = client.infer(
            model_name=self._model,
            inputs=inputs,
            outputs=outputs,
        )
        return np.asarray(result.as_numpy(self._output_name), dtype=np.float32)

    def _postprocess(self, raw: np.ndarray) -> list[PlateDetection]:
        """Convert Triton output into filtered normalized detections."""
        preds = _coerce_predictions(raw)
        if preds.size == 0:
            return []

        cx = preds[:, 0].astype(np.float32)
        cy = preds[:, 1].astype(np.float32)
        w = preds[:, 2].astype(np.float32)
        h = preds[:, 3].astype(np.float32)
        conf = preds[:, 4].astype(np.float32)

        if np.max(np.abs(preds[:, :4])) > 1.5:
            cx = cx / float(self._input_size)
            cy = cy / float(self._input_size)
            w = w / float(self._input_size)
            h = h / float(self._input_size)

        mask = conf >= self._confidence_threshold
        if not np.any(mask):
            return []

        cx = cx[mask]
        cy = cy[mask]
        w = w[mask]
        h = h[mask]
        conf = conf[mask]

        x1 = np.clip(cx - (w / 2.0), 0.0, 1.0)
        y1 = np.clip(cy - (h / 2.0), 0.0, 1.0)
        x2 = np.clip(cx + (w / 2.0), 0.0, 1.0)
        y2 = np.clip(cy + (h / 2.0), 0.0, 1.0)

        keep = _nms(x1, y1, x2, y2, conf, self._nms_iou_threshold)
        detections: list[PlateDetection] = []
        for idx in keep:
            detections.append(
                PlateDetection(
                    x=float(x1[idx]),
                    y=float(y1[idx]),
                    w=float(max(0.0, x2[idx] - x1[idx])),
                    h=float(max(0.0, y2[idx] - y1[idx])),
                    confidence=float(conf[idx]),
                )
            )
        return detections


def _coerce_predictions(raw: np.ndarray) -> np.ndarray:
    if raw.size == 0:
        return np.empty((0, 5), dtype=np.float32)
    array = np.asarray(raw)
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 2:
        return np.empty((0, 5), dtype=np.float32)
    if array.shape[1] < 5 and array.shape[0] >= 5:
        array = array.T
    if array.shape[1] < 5:
        return np.empty((0, 5), dtype=np.float32)
    return array[:, :5].astype(np.float32)


def _nms(
    x1: np.ndarray,
    y1: np.ndarray,
    x2: np.ndarray,
    y2: np.ndarray,
    scores: np.ndarray,
    iou_thresh: float,
) -> list[int]:
    """Greedy NMS over normalized boxes."""
    if scores.size == 0:
        return []

    order = scores.argsort()[::-1]
    keep: list[int] = []

    while order.size > 0:
        index = int(order[0])
        keep.append(index)
        if order.size == 1:
            break

        xx1 = np.maximum(x1[index], x1[order[1:]])
        yy1 = np.maximum(y1[index], y1[order[1:]])
        xx2 = np.minimum(x2[index], x2[order[1:]])
        yy2 = np.minimum(y2[index], y2[order[1:]])

        inter_w = np.maximum(0.0, xx2 - xx1)
        inter_h = np.maximum(0.0, yy2 - yy1)
        intersection = inter_w * inter_h

        area_index = max(0.0, (x2[index] - x1[index]) * (y2[index] - y1[index]))
        area_other = np.maximum(0.0, (x2[order[1:]] - x1[order[1:]]) * (y2[order[1:]] - y1[order[1:]]))
        union = area_index + area_other - intersection
        iou = np.where(union > 0.0, intersection / union, 0.0)

        remaining = np.where(iou <= iou_thresh)[0]
        order = order[remaining + 1]

    return keep
