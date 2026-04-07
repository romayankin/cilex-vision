from __future__ import annotations

import importlib
import sys
from pathlib import Path

import yaml


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

cost_model = importlib.import_module("cost_model")


def test_params_yaml_keeps_calibration_contract_and_adds_cost_model() -> None:
    params_path = SCRIPT_DIR / "params.yaml"
    payload = yaml.safe_load(params_path.read_text(encoding="utf-8"))

    assert payload["defaults"]["target_pass_through_rate"] == 0.15
    assert payload["defaults"]["capture_window_s"] == 600
    assert payload["cameras"] == {}
    assert "cost_model" in payload
    assert payload["cost_model"]["assumption_marker"] == cost_model.ASSUMPTION_MARKER


def test_load_topic_catalog_matches_repo_contract() -> None:
    topics = cost_model.load_topic_catalog(cost_model.DEFAULT_TOPICS_PATH)

    assert len(topics) == 7
    assert sum(topic.partitions for topic in topics) == 56
    assert {topic.name for topic in topics} == {
        "frames.sampled.refs",
        "tracklets.local",
        "attributes.jobs",
        "mtmc.active_embeddings",
        "events.raw",
        "archive.transcode.requested",
        "archive.transcode.completed",
    }


def test_build_reports_produces_requested_matrix() -> None:
    inputs = cost_model.load_cost_model_inputs(cost_model.DEFAULT_PARAMS_PATH)
    topics = cost_model.load_topic_catalog(cost_model.DEFAULT_TOPICS_PATH)
    inventory = cost_model.load_compose_inventory(cost_model.DEFAULT_COMPOSE_PATH)

    reports = cost_model.build_reports(inputs=inputs, topics=topics, inventory=inventory)

    assert [report.scenario.name for report in reports] == ["P25", "P50", "P90"]
    assert [row.cameras for row in reports[0].summary_rows] == [4, 10, 100]

    p50_100 = next(row for row in reports[1].summary_rows if row.cameras == 100)
    p90_100 = next(row for row in reports[2].summary_rows if row.cameras == 100)

    assert p50_100.gpu_nodes_needed == 1
    assert p90_100.gpu_nodes_needed == 2
    assert p90_100.total_monthly_usd > p50_100.total_monthly_usd
    assert p50_100.kafka_storage_gb > 0.0


def test_render_stdout_report_mentions_all_scenarios() -> None:
    inputs = cost_model.load_cost_model_inputs(cost_model.DEFAULT_PARAMS_PATH)
    topics = cost_model.load_topic_catalog(cost_model.DEFAULT_TOPICS_PATH)
    inventory = cost_model.load_compose_inventory(cost_model.DEFAULT_COMPOSE_PATH)
    reports = cost_model.build_reports(inputs=inputs, topics=topics, inventory=inventory)

    output = cost_model.render_stdout_report(
        reports=reports,
        inputs=inputs,
        topics=topics,
        inventory=inventory,
        topics_path=cost_model.DEFAULT_TOPICS_PATH,
        compose_path=cost_model.DEFAULT_COMPOSE_PATH,
        xlsx_output=None,
    )

    assert "Scenario P25" in output
    assert "Scenario P50" in output
    assert "Scenario P90" in output
    assert "| 100" in output


def test_write_excel_workbook_creates_xlsx(tmp_path: Path) -> None:
    inputs = cost_model.load_cost_model_inputs(cost_model.DEFAULT_PARAMS_PATH)
    topics = cost_model.load_topic_catalog(cost_model.DEFAULT_TOPICS_PATH)
    inventory = cost_model.load_compose_inventory(cost_model.DEFAULT_COMPOSE_PATH)
    reports = cost_model.build_reports(inputs=inputs, topics=topics, inventory=inventory)

    output_path = tmp_path / "cost-model.xlsx"
    cost_model.write_excel_workbook(
        path=output_path,
        reports=reports,
        inputs=inputs,
        topics=topics,
        inventory=inventory,
    )

    assert output_path.exists()
