# Triton Shadow Model Repository

This directory is the staging area for candidate TensorRT engines before a
shadow deployment.

## Purpose

- Keep candidate engines separate from the production model repository under
  `infra/triton/models/`.
- Mirror the same per-model, per-version layout Triton expects.
- Support the Stage 2 shadow deployment flow from
  `docs/runbooks/model-rollout-sop.md` without touching production topics.

## Layout

The directory structure mirrors the production repository:

```text
infra/triton/shadow/
├── yolov8l/
│   └── 2/
│       └── model.plan
├── osnet/
│   └── 2/
│       └── model.plan
└── color_classifier/
    └── 2/
        └── model.plan
```

Each model directory should contain:

- `config.pbtxt` only if the candidate needs a different Triton config
  than production.
- numeric version directories (`1/`, `2/`, ...) containing `model.plan`.

## Lifecycle

1. Place the candidate `model.plan` in this shadow repository.
2. Use `scripts/shadow/deploy_shadow_model.py` to copy the engine into the
   active Triton model repository.
3. Load the model through Triton's EXPLICIT control API:
   `POST /v2/repository/models/{name}/load`.
4. Run `scripts/shadow/shadow_inference_worker.py` against the explicit
   candidate version and publish only to shadow Kafka topics.
5. Compare production and shadow behavior with
   `scripts/shadow/compare_shadow.py`.
6. Promote by cutover or roll back by unloading the candidate version.

## Notes

- Shadow deployment relies on `version_policy { latest { num_versions: 2 } }`
  in the production `config.pbtxt`.
- Rollback removes the candidate version directory from the active model
  repository, unloads the model, and reloads the remaining versions.
- Shadow data is intentionally ephemeral; keep it out of production topics and
  durable downstream pipelines.
