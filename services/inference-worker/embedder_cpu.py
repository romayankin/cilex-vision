"""CPU Re-ID embedder using OSNet-x0.25 via torchreid.

Replaces the zero-vector stub. Produces real 512-d L2-normalised
embeddings that enable cross-camera Re-ID, similarity search,
MTMC association, and meaningful events.

OSNet-x0.25: 0.5M params, ~5ms/crop on CPU, 90.8% Rank-1 on Market-1501.
Preprocessing matches EmbedderClient (Triton) — ImageNet normalisation,
bilinear resize to 256x128, NCHW float32.

Per ADR-008, embeddings from different model versions MUST NOT be
compared. The existing zero-vector embeddings in the database are
incompatible with the real OSNet-x0.25 output.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
from PIL import Image

from metrics import EMBEDDING_LATENCY

logger = logging.getLogger(__name__)

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

EMBED_H = 256
EMBED_W = 128
EMBED_DIM = 512


class CpuEmbedderClient:
    """OSNet-x0.25 CPU embedder for person Re-ID."""

    EMBED_DIM = EMBED_DIM

    def __init__(self) -> None:
        self._model = None  # lazy init

    def _get_model(self):
        if self._model is not None:
            return self._model

        try:
            import torchreid  # noqa: PLC0415
        except ImportError as exc:
            logger.error(
                "torchreid not installed — cannot load OSNet-x0.25. "
                "Install with: pip install torchreid"
            )
            raise RuntimeError("torchreid required for CPU Re-ID embedder") from exc

        # Build model with 751 classes (Market-1501 training identities) and
        # pretrained=False so torchreid does NOT download the ImageNet backbone.
        # We load Market-1501 Re-ID weights manually below.
        model = torchreid.models.build_model(
            name="osnet_x0_25",
            num_classes=751,
            loss="softmax",
            pretrained=False,
        )

        # Download Market-1501 trained weights from the torchreid Model Zoo.
        # osnet_x0_25 on Market-1501: 91.2% Rank-1, 75.0% mAP.
        import os  # noqa: PLC0415

        import gdown  # noqa: PLC0415

        cache_dir = os.path.join(
            os.path.expanduser(os.environ.get("TORCH_HOME", "~/.cache/torch")),
            "checkpoints",
        )
        os.makedirs(cache_dir, exist_ok=True)
        weight_path = os.path.join(cache_dir, "osnet_x0_25_market1501.pth.tar")

        if not os.path.exists(weight_path):
            logger.info("Downloading OSNet-x0.25 Market-1501 weights...")
            gdown.download(
                "https://drive.google.com/uc?id=1z1UghYvOTtjx7kEoRfmqSMu-z62J6MAj",
                weight_path,
                quiet=False,
            )

        torchreid.utils.load_pretrained_weights(model, weight_path)
        model.eval()

        # Remove stale ImageNet cache from the previous implementation.
        imagenet_cache = os.path.join(cache_dir, "osnet_x0_25_imagenet.pth")
        if os.path.exists(imagenet_cache):
            os.remove(imagenet_cache)
            logger.info("Removed stale ImageNet weights: %s", imagenet_cache)

        feature_dim = getattr(model, "feature_dim", None)
        logger.info(
            "Loaded OSNet-x0.25 Market-1501 Re-ID weights (91.2%% Rank-1) "
            "from %s (feature_dim=%s)",
            weight_path,
            feature_dim,
        )

        self._model = model

        # Self-test: real embedding, L2-normalised, non-zero
        test_input = np.random.randint(
            0, 255, (EMBED_H, EMBED_W, 3), dtype=np.uint8
        )
        test_tensor = self._preprocess(test_input)
        test_emb = self._infer(test_tensor)
        assert test_emb.shape == (EMBED_DIM,), (
            f"Expected ({EMBED_DIM},), got {test_emb.shape}"
        )
        test_norm = float(np.linalg.norm(test_emb))
        assert test_norm > 0.99, (
            f"Embedding must be L2-normalised (non-zero), got norm={test_norm:.4f}"
        )
        logger.info(
            "OSNet-x0.25 self-test passed: %d-d embedding, norm=%.4f",
            len(test_emb),
            test_norm,
        )
        return model

    async def extract(
        self,
        frame: np.ndarray,
        bbox: tuple[float, float, float, float],
    ) -> np.ndarray:
        """Extract a 512-d L2-normalised embedding from a detection crop."""
        # Ensure model loaded before timing the inference
        self._get_model()

        crop = self._crop(frame, bbox)
        tensor = self._preprocess(crop)

        t0 = time.monotonic()
        embedding = await asyncio.to_thread(self._infer, tensor)
        elapsed_ms = (time.monotonic() - t0) * 1000
        EMBEDDING_LATENCY.observe(elapsed_ms)

        return embedding

    @staticmethod
    def _crop(
        frame: np.ndarray,
        bbox: tuple[float, float, float, float],
    ) -> np.ndarray:
        """Crop detection region from frame (mirrors EmbedderClient)."""
        h, w = frame.shape[:2]
        x1 = max(0, int(bbox[0] * w))
        y1 = max(0, int(bbox[1] * h))
        x2 = min(w, int(bbox[2] * w))
        y2 = min(h, int(bbox[3] * h))
        if x2 <= x1 or y2 <= y1:
            return np.zeros((EMBED_H, EMBED_W, 3), dtype=np.uint8)
        return frame[y1:y2, x1:x2].copy()

    @staticmethod
    def _preprocess(crop: np.ndarray) -> np.ndarray:
        """Resize + ImageNet normalise + NCHW batch (mirrors EmbedderClient)."""
        pil_img = Image.fromarray(crop)
        resized = pil_img.resize((EMBED_W, EMBED_H), Image.BILINEAR)
        arr = np.array(resized, dtype=np.float32) / 255.0
        arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
        tensor = arr.transpose(2, 0, 1)  # HWC → CHW
        return np.expand_dims(tensor, axis=0).astype(np.float32)

    def _infer(self, input_tensor: np.ndarray) -> np.ndarray:
        """Run OSNet forward pass on CPU, return L2-normalised 512-d vector."""
        import torch  # noqa: PLC0415

        model = self._model
        with torch.no_grad():
            t = torch.from_numpy(input_tensor)
            features = model(t)
            if isinstance(features, (tuple, list)):
                features = features[0]
            embedding = features.cpu().numpy().flatten()

        norm = float(np.linalg.norm(embedding))
        if norm > 0:
            embedding = embedding / norm

        if len(embedding) < EMBED_DIM:
            embedding = np.pad(embedding, (0, EMBED_DIM - len(embedding)))
        elif len(embedding) > EMBED_DIM:
            embedding = embedding[:EMBED_DIM]

        return embedding.astype(np.float32)
