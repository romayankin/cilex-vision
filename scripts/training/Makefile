.PHONY: pull-data version-data train evaluate validate export promote retrain

PYTHON ?= python3
MLFLOW_URI ?= http://localhost:5000
CVAT_URL ?= http://localhost:8080
DATA_VERSION ?= v1
MODEL_NAME ?= yolov8l-detector
VALIDATION_OUTPUT_DIR ?= ../../artifacts/evaluation/retraining-validation
VALIDATION_REPORT ?= $(VALIDATION_OUTPUT_DIR)/validation-report.md
VALIDATE_ARGS ?=

pull-data:
	$(PYTHON) pull_data.py --cvat-url $(CVAT_URL) --project detection-eval --output-dir data/training/raw

version-data:
	$(PYTHON) version_data.py --data-dir data/training/raw --version $(DATA_VERSION)

train:
	$(PYTHON) train.py mlflow.tracking_uri=$(MLFLOW_URI)

evaluate:
	$(PYTHON) evaluate.py --mlflow-uri $(MLFLOW_URI)

validate:
	$(PYTHON) ../evaluation/validate_retrained_model.py \
		--mlflow-uri $(MLFLOW_URI) \
		--regression-threshold 0.02 \
		--output-dir $(VALIDATION_OUTPUT_DIR) \
		--report-path $(VALIDATION_REPORT) \
		$(VALIDATE_ARGS)

export:
	$(PYTHON) export.py --output-dir models/exported

promote:
	$(PYTHON) promote.py --mlflow-uri $(MLFLOW_URI) --model-name $(MODEL_NAME) --stage Staging

retrain: pull-data version-data train evaluate export
	@echo "Retraining pipeline complete. Run 'make promote' after manual review."
