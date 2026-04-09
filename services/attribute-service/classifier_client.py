"""Triton gRPC client for color classification.

Follows the inference-worker/detector_client.py pattern: lazy import of
``tritonclient.grpc``, synchronous ``_infer()`` wrapped in
``asyncio.to_thread()``.

Preprocessing: resize to 224x224, BGR->RGB, normalize to [0,1], HWC->CHW.
Output: 10-class softmax mapped to taxonomy color names.
"""

from __future__ import annotations

import asyncio
import logging
import time

import cv2
import numpy as np

from metrics import CLASSIFICATION_LATENCY

logger = logging.getLogger(__name__)

# Color index order matches Triton output and proto Color enum (1-indexed).
# Index in the 10-element output vector:
COLOR_INDEX_TO_NAME: dict[int, str] = {
    0: "red",
    1: "blue",
    2: "white",
    3: "black",
    4: "silver",
    5: "green",
    6: "yellow",
    7: "brown",
    8: "orange",
    9: "unknown",
}


def _softmax(x: np.ndarray) -> np.ndarray:
    """Numerically stable softmax."""
    e = np.exp(x - np.max(x))
    return e / e.sum()


class ClassifierClient:
    """Triton gRPC client for ResNet-18 color classification."""

    def __init__(
        self,
        triton_url: str,
        model_name: str = "color_classifier",
        input_name: str = "images",
        output_name: str = "probabilities",
        confidence_threshold: float = 0.30,
    ) -> None:
        self._url = triton_url
        self._model = model_name
        self._input_name = input_name
        self._output_name = output_name
        self._threshold = confidence_threshold
        self._client = None  # lazy init

    def _get_client(self) -> object:
        if self._client is None:
            import tritonclient.grpc as grpcclient  # noqa: PLC0415
            self._client = grpcclient.InferenceServerClient(url=self._url)
        return self._client

    async def classify(self, crop_bgr: np.ndarray) -> tuple[str, float]:
        """Classify the dominant color of a BGR crop.

        Returns (color_name, confidence). If max confidence < threshold,
        returns ("unknown", max_confidence).
        """
        input_tensor = self._preprocess(crop_bgr)

        t0 = time.monotonic()
        raw_output = await asyncio.to_thread(self._infer, input_tensor)
        elapsed_ms = (time.monotonic() - t0) * 1000
        CLASSIFICATION_LATENCY.observe(elapsed_ms)

        return self._postprocess(raw_output)

    def _preprocess(self, crop_bgr: np.ndarray) -> np.ndarray:
        """Resize to 224x224, BGR->RGB, normalize to [0,1], HWC->CHW."""
        resized = cv2.resize(crop_bgr, (224, 224), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        tensor = rgb.astype(np.float32) / 255.0
        # HWC -> CHW
        tensor = tensor.transpose(2, 0, 1)
        # Add batch dim: (1, 3, 224, 224)
        return np.expand_dims(tensor, axis=0)

    def _infer(self, input_tensor: np.ndarray) -> np.ndarray:
        """Synchronous Triton gRPC inference (called via to_thread)."""
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
        result = client.infer(  # type: ignore[union-attr]
            model_name=self._model,
            inputs=inputs,
            outputs=outputs,
        )
        return result.as_numpy(self._output_name)

    def _postprocess(self, raw: np.ndarray) -> tuple[str, float]:
        """Softmax + map to color name."""
        logits = raw[0]  # (10,)
        probs = _softmax(logits)

        # Among named colors (indices 0-8), find the best
        named_probs = probs[:9]
        best_idx = int(np.argmax(named_probs))
        best_conf = float(named_probs[best_idx])

        if best_conf < self._threshold:
            return ("unknown", best_conf)

        return (COLOR_INDEX_TO_NAME[best_idx], best_conf)
