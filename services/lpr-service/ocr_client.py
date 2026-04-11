"""Triton gRPC client for license plate OCR."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from metrics import OCR_LATENCY_MS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrResult:
    """Decoded plate text and confidence summary."""

    text: str
    confidence: float
    country_format: str | None
    character_confidences: list[float]


class OcrClient:
    """OCR client for plate recognition."""

    def __init__(
        self,
        *,
        triton_url: str,
        model_name: str,
        input_name: str,
        output_name: str,
        input_width: int,
        input_height: int,
        alphabet: str,
        confidence_threshold: float,
    ) -> None:
        self._url = triton_url
        self._model = model_name
        self._input_name = input_name
        self._output_name = output_name
        self._input_width = input_width
        self._input_height = input_height
        self._alphabet = alphabet
        self._confidence_threshold = confidence_threshold
        self._client: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model

    def _get_client(self) -> Any:
        if self._client is None:
            import tritonclient.grpc as grpcclient  # noqa: PLC0415

            self._client = grpcclient.InferenceServerClient(url=self._url)
        return self._client

    async def recognize(self, plate_crop_rgb: np.ndarray) -> OcrResult:
        """Recognize text from a plate crop."""
        tensor = self._preprocess(plate_crop_rgb)
        started = time.monotonic()
        raw = await asyncio.to_thread(self._infer, tensor)
        OCR_LATENCY_MS.observe((time.monotonic() - started) * 1000.0)
        return self._postprocess(raw)

    def _preprocess(self, plate_crop_rgb: np.ndarray) -> np.ndarray:
        image = Image.fromarray(plate_crop_rgb)
        resized = image.resize((self._input_width, self._input_height), Image.BILINEAR)
        array = np.asarray(resized, dtype=np.float32) / 255.0
        tensor = array.transpose(2, 0, 1)
        return np.expand_dims(tensor, axis=0)

    def _infer(self, input_tensor: np.ndarray) -> np.ndarray:
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

    def _postprocess(self, raw: np.ndarray) -> OcrResult:
        array = np.asarray(raw)
        if array.size == 0:
            return OcrResult("", 0.0, None, [])
        if array.ndim == 3 and array.shape[0] == 1:
            array = array[0]
        if array.ndim == 1:
            indices = [int(value) for value in array.tolist()]
            return self._decode_indices(indices)
        if array.ndim != 2:
            return OcrResult("", 0.0, None, [])
        return self._decode_ctc_logits(array)

    def _decode_ctc_logits(self, logits: np.ndarray) -> OcrResult:
        probabilities = _softmax(logits.astype(np.float32), axis=1)
        sequence = np.argmax(probabilities, axis=1)
        timestep_conf = probabilities[np.arange(probabilities.shape[0]), sequence]

        decoded_chars: list[str] = []
        char_confidences: list[float] = []
        previous = -1
        for index, confidence in zip(sequence.tolist(), timestep_conf.tolist(), strict=True):
            if index == 0 or index == previous:
                previous = index
                continue
            char_index = index - 1
            if 0 <= char_index < len(self._alphabet):
                decoded_chars.append(self._alphabet[char_index])
                char_confidences.append(float(confidence))
            previous = index

        text = "".join(decoded_chars)
        confidence = float(np.mean(char_confidences)) if char_confidences else 0.0
        if confidence < self._confidence_threshold:
            return OcrResult("", confidence, None, char_confidences)
        return OcrResult(
            text=text,
            confidence=confidence,
            country_format=_infer_country_format(text),
            character_confidences=char_confidences,
        )

    def _decode_indices(self, indices: list[int]) -> OcrResult:
        chars: list[str] = []
        for index in indices:
            if index <= 0:
                continue
            char_index = index - 1
            if 0 <= char_index < len(self._alphabet):
                chars.append(self._alphabet[char_index])
        text = "".join(chars)
        confidence = 1.0 if text else 0.0
        if confidence < self._confidence_threshold:
            return OcrResult("", confidence, None, [])
        return OcrResult(text, confidence, _infer_country_format(text), [])


def _softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    shifted = x - np.max(x, axis=axis, keepdims=True)
    exp = np.exp(shifted)
    return np.asarray(exp / np.sum(exp, axis=axis, keepdims=True), dtype=np.float32)


def _infer_country_format(text: str) -> str | None:
    if not text:
        return None
    if re.fullmatch(r"[A-Z]{3}[0-9]{3}", text):
        return "latin-3l-3d"
    if re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z]{3}", text):
        return "latin-2l-2d-3l"
    if re.fullmatch(r"[0-9]{6,8}", text):
        return "numeric"
    if re.fullmatch(r"[A-Z0-9]{5,8}", text):
        return "generic-alphanumeric"
    return "unknown"
