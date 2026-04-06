"""Triton gRPC client for OSNet Re-ID embedding extraction.

Preprocesses detection crops with ImageNet normalisation and bilinear
resize to 256×128, then sends to Triton for inference.

Embeddings are L2-normalised 512-d vectors.  Per ADR-008, embeddings
from different model versions MUST NOT be compared.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from config import TritonConfig
from metrics import EMBEDDING_LATENCY

logger = logging.getLogger(__name__)

# ImageNet normalisation constants
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class EmbedderClient:
    """Triton gRPC client for OSNet Re-ID inference."""

    EMBED_H = 256
    EMBED_W = 128
    EMBED_DIM = 512

    def __init__(self, triton_cfg: TritonConfig) -> None:
        self._cfg = triton_cfg
        self._client = None  # lazy init

    def _get_client(self) -> object:
        if self._client is None:
            import tritonclient.grpc as grpcclient  # noqa: PLC0415

            self._client = grpcclient.InferenceServerClient(
                url=self._cfg.url,
            )
        return self._client

    async def extract(
        self,
        frame: np.ndarray,
        bbox: tuple[float, float, float, float],
    ) -> np.ndarray:
        """Extract a 512-d embedding from a detection crop.

        Args:
            frame: Full RGB frame (H, W, 3) uint8.
            bbox: Normalised (x_min, y_min, x_max, y_max).

        Returns:
            L2-normalised embedding vector of shape (512,).
        """
        crop = self._crop(frame, bbox)
        tensor = self._preprocess(crop)

        t0 = time.monotonic()
        raw = await asyncio.to_thread(self._infer, tensor)
        elapsed_ms = (time.monotonic() - t0) * 1000
        EMBEDDING_LATENCY.observe(elapsed_ms)

        # L2 normalise
        embedding = raw[0]
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm
        return embedding

    # ------------------------------------------------------------------
    # Preprocessing
    # ------------------------------------------------------------------

    @staticmethod
    def _crop(
        frame: np.ndarray,
        bbox: tuple[float, float, float, float],
    ) -> np.ndarray:
        """Crop the detection region from the frame."""
        h, w = frame.shape[:2]
        x1 = max(0, int(bbox[0] * w))
        y1 = max(0, int(bbox[1] * h))
        x2 = min(w, int(bbox[2] * w))
        y2 = min(h, int(bbox[3] * h))

        if x2 <= x1 or y2 <= y1:
            return np.zeros((EmbedderClient.EMBED_H, EmbedderClient.EMBED_W, 3), dtype=np.uint8)

        return frame[y1:y2, x1:x2].copy()

    def _preprocess(self, crop: np.ndarray) -> np.ndarray:
        """Resize, normalise with ImageNet stats, NCHW layout."""
        from PIL import Image  # noqa: PLC0415

        pil_img = Image.fromarray(crop)
        resized = pil_img.resize(
            (self.EMBED_W, self.EMBED_H), Image.BILINEAR
        )
        arr = np.array(resized, dtype=np.float32) / 255.0

        # ImageNet normalisation
        arr = (arr - IMAGENET_MEAN) / IMAGENET_STD

        # HWC → CHW, add batch dim
        tensor = arr.transpose(2, 0, 1)
        return np.expand_dims(tensor, axis=0).astype(np.float32)

    # ------------------------------------------------------------------
    # Triton inference
    # ------------------------------------------------------------------

    def _infer(self, input_tensor: np.ndarray) -> np.ndarray:
        """Synchronous Triton gRPC inference (called via to_thread)."""
        import tritonclient.grpc as grpcclient  # noqa: PLC0415

        client = self._get_client()
        inputs = [
            grpcclient.InferInput(
                self._cfg.embedder_input_name,
                list(input_tensor.shape),
                "FP32",
            )
        ]
        inputs[0].set_data_from_numpy(input_tensor)
        outputs = [
            grpcclient.InferRequestedOutput(
                self._cfg.embedder_output_name,
            )
        ]
        result = client.infer(  # type: ignore[union-attr]
            model_name=self._cfg.embedder_model,
            inputs=inputs,
            outputs=outputs,
        )
        return result.as_numpy(self._cfg.embedder_output_name)
