#!/usr/bin/env python3
"""Hydra-configured training script for detector fine-tuning.

Provides the training loop structure with MLflow experiment tracking,
Hydra config management, and early stopping. The actual model forward
pass and dataset loading are abstracted behind clearly documented
interfaces so that the real PyTorch implementation slots in directly.

Usage:
    python train.py                           # defaults from conf/config.yaml
    python train.py training.epochs=50        # Hydra override
    python train.py --config-name custom      # alternate config
    python train.py mlflow.tracking_uri=http://mlflow:5000
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hydra
import mlflow
from omegaconf import DictConfig, OmegaConf

log = logging.getLogger(__name__)

OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)


# ---------------------------------------------------------------------------
# Data loading (interface — real implementation uses PyTorch DataLoader)
# ---------------------------------------------------------------------------


@dataclass
class DatasetInfo:
    manifest_path: str
    item_count: int = 0
    classes: list[str] = field(default_factory=list)


def load_manifest(path: str) -> dict[str, Any]:
    """Load a dataset manifest JSON file.

    Expected shape:
    {
        "items": [
            {"item_id": "...", "camera_id": "...", "capture_ts": "...",
             "sequence_id": "...", "source_uri": "..."}
        ]
    }
    """
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")

    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)

    return data


def prepare_datasets(cfg: DictConfig) -> tuple[DatasetInfo, DatasetInfo, DatasetInfo]:
    """Load and validate train/val/test manifests.

    Returns DatasetInfo objects. In a full implementation, these would
    wrap PyTorch Dataset/DataLoader instances.
    """
    splits: list[DatasetInfo] = []
    for split_name, manifest_key in [
        ("train", cfg.data.train_manifest),
        ("val", cfg.data.val_manifest),
        ("test", cfg.data.test_manifest),
    ]:
        try:
            manifest = load_manifest(manifest_key)
            items = manifest.get("items", [])
            info = DatasetInfo(
                manifest_path=manifest_key,
                item_count=len(items),
                classes=list(OBJECT_CLASSES),
            )
            log.info("loaded %s split: %d items from %s", split_name, info.item_count, manifest_key)
        except FileNotFoundError:
            log.warning("manifest not found for %s split: %s (skeleton mode)", split_name, manifest_key)
            info = DatasetInfo(manifest_path=manifest_key, item_count=0, classes=list(OBJECT_CLASSES))
        splits.append(info)

    return splits[0], splits[1], splits[2]


# ---------------------------------------------------------------------------
# Model initialization (interface — real implementation uses ultralytics/torch)
# ---------------------------------------------------------------------------


@dataclass
class ModelState:
    name: str
    num_classes: int
    input_size: int
    pretrained: bool
    epoch: int = 0
    best_map: float = 0.0
    best_checkpoint: str | None = None


def init_model(cfg: DictConfig) -> ModelState:
    """Initialize the detection model.

    In a full implementation, this would load a YOLOv8 model via
    ultralytics or a custom PyTorch module. Here we document the
    interface and return a state tracker.
    """
    state = ModelState(
        name=cfg.model.name,
        num_classes=cfg.model.num_classes,
        input_size=cfg.model.input_size,
        pretrained=cfg.model.pretrained,
    )
    log.info(
        "initialized model: %s (classes=%d, input=%d, pretrained=%s)",
        state.name, state.num_classes, state.input_size, state.pretrained,
    )
    return state


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------


@dataclass
class EpochMetrics:
    epoch: int
    train_loss: float
    val_loss: float
    val_map50: float
    val_map50_95: float
    learning_rate: float


def train_one_epoch(
    model: ModelState,
    epoch: int,
    cfg: DictConfig,
) -> EpochMetrics:
    """Run one training epoch.

    Skeleton implementation that documents the expected interface.
    A real implementation would:
    1. Iterate over train DataLoader batches
    2. Forward pass, loss computation, backward pass
    3. Optimizer step with LR scheduler
    4. Run validation at end of epoch
    5. Compute mAP metrics on validation set
    """
    # Placeholder: in production, replace with actual training logic
    # The structure is preserved so real code slots in at this exact point
    progress_fraction = (epoch + 1) / cfg.training.epochs

    metrics = EpochMetrics(
        epoch=epoch,
        train_loss=1.0 / (1.0 + progress_fraction * 10),
        val_loss=1.0 / (1.0 + progress_fraction * 8),
        val_map50=min(0.95, 0.3 + progress_fraction * 0.6),
        val_map50_95=min(0.75, 0.2 + progress_fraction * 0.5),
        learning_rate=cfg.training.learning_rate * (1.0 - progress_fraction * 0.9),
    )

    return metrics


def should_stop_early(
    metrics_history: list[EpochMetrics],
    patience: int,
) -> bool:
    """Check if training should stop based on validation mAP plateau."""
    if len(metrics_history) < patience + 1:
        return False

    recent = metrics_history[-patience:]
    best_recent = max(m.val_map50_95 for m in recent)
    best_before = max(m.val_map50_95 for m in metrics_history[:-patience])

    return best_recent <= best_before


def save_checkpoint(model: ModelState, epoch: int, output_dir: Path) -> str:
    """Save a model checkpoint.

    In production, this serializes the PyTorch model state_dict.
    Here we create a metadata JSON as a placeholder.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / f"checkpoint_epoch_{epoch:04d}.json"
    checkpoint_data = {
        "model_name": model.name,
        "epoch": epoch,
        "best_map": model.best_map,
        "num_classes": model.num_classes,
        "input_size": model.input_size,
    }
    checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2) + "\n", encoding="utf-8")
    return str(checkpoint_path)


# ---------------------------------------------------------------------------
# MLflow integration
# ---------------------------------------------------------------------------


def setup_mlflow(cfg: DictConfig) -> str:
    """Configure MLflow tracking and start a run."""
    mlflow.set_tracking_uri(cfg.mlflow.tracking_uri)
    mlflow.set_experiment(cfg.mlflow.experiment_name)

    run_name = cfg.mlflow.run_name
    if not run_name:
        run_name = f"{cfg.model.name}-{int(time.time())}"

    return run_name


def log_config_to_mlflow(cfg: DictConfig) -> None:
    """Log all Hydra config parameters to MLflow."""
    flat = {}

    def _flatten(d: Any, prefix: str = "") -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                _flatten(v, f"{prefix}{k}.")
        elif isinstance(d, list):
            flat[prefix.rstrip(".")] = str(d)
        else:
            flat[prefix.rstrip(".")] = d

    _flatten(OmegaConf.to_container(cfg, resolve=True))
    mlflow.log_params(flat)


# ---------------------------------------------------------------------------
# Hydra entry point
# ---------------------------------------------------------------------------


@hydra.main(config_path="conf", config_name="config", version_base=None)
def train(cfg: DictConfig) -> None:
    """Main training entry point, decorated with Hydra for config management."""
    log.info("starting training with config:\n%s", OmegaConf.to_yaml(cfg))

    # Prepare data
    train_data, val_data, test_data = prepare_datasets(cfg)

    # Initialize model
    model = init_model(cfg)

    # Setup MLflow
    run_name = setup_mlflow(cfg)

    # Resolve output directory (Hydra changes cwd)
    original_cwd = hydra.utils.get_original_cwd()
    output_dir = Path(original_cwd) / "models" / "checkpoints"

    with mlflow.start_run(run_name=run_name) as run:
        log.info("MLflow run: %s (id: %s)", run_name, run.info.run_id)

        # Log config
        log_config_to_mlflow(cfg)
        mlflow.log_param("train_items", train_data.item_count)
        mlflow.log_param("val_items", val_data.item_count)

        # Training loop
        metrics_history: list[EpochMetrics] = []

        for epoch in range(cfg.training.epochs):
            metrics = train_one_epoch(model, epoch, cfg)
            metrics_history.append(metrics)

            # Log epoch metrics
            mlflow.log_metrics(
                {
                    "train_loss": metrics.train_loss,
                    "val_loss": metrics.val_loss,
                    "val_mAP@0.5": metrics.val_map50,
                    "val_mAP@0.5:0.95": metrics.val_map50_95,
                    "learning_rate": metrics.learning_rate,
                },
                step=epoch,
            )

            # Save best checkpoint
            if metrics.val_map50_95 > model.best_map:
                model.best_map = metrics.val_map50_95
                model.epoch = epoch
                checkpoint_path = save_checkpoint(model, epoch, output_dir)
                model.best_checkpoint = checkpoint_path
                log.info(
                    "epoch %d: new best mAP@0.5:0.95=%.4f, saved %s",
                    epoch, metrics.val_map50_95, checkpoint_path,
                )

            # Early stopping
            if should_stop_early(metrics_history, cfg.training.early_stopping_patience):
                log.info("early stopping at epoch %d (patience=%d)", epoch, cfg.training.early_stopping_patience)
                break

            if epoch % 10 == 0 or epoch == cfg.training.epochs - 1:
                log.info(
                    "epoch %d/%d: loss=%.4f val_loss=%.4f mAP@0.5=%.4f mAP@0.5:0.95=%.4f",
                    epoch, cfg.training.epochs, metrics.train_loss, metrics.val_loss,
                    metrics.val_map50, metrics.val_map50_95,
                )

        # Log final metrics
        if model.best_checkpoint:
            mlflow.log_artifact(model.best_checkpoint)

        mlflow.log_metrics({
            "best_mAP@0.5:0.95": model.best_map,
            "best_epoch": model.epoch,
            "total_epochs": len(metrics_history),
        })

        # Tag run for downstream use
        mlflow.set_tag("model_name", cfg.model.name)
        mlflow.set_tag("num_classes", str(cfg.model.num_classes))

        log.info(
            "training complete: best mAP@0.5:0.95=%.4f at epoch %d (run_id=%s)",
            model.best_map, model.epoch, run.info.run_id,
        )

        # Write run ID for downstream scripts
        run_id_path = Path(original_cwd) / "models" / "latest_run_id.txt"
        run_id_path.parent.mkdir(parents=True, exist_ok=True)
        run_id_path.write_text(run.info.run_id + "\n", encoding="utf-8")


def main() -> None:
    train()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
