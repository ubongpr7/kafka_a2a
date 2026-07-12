from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_script_module(name: str, relative_path: str):
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / relative_path
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_generated_a2a_eval_corpus_has_300_questions() -> None:
    module = _load_script_module("a2a_eval_corpus_test", "scripts/a2a_eval_corpus.py")

    scenarios = module.GENERATED_SCENARIOS
    suites = module.GENERATED_SUITES

    assert len(scenarios) == 300
    assert len(suites["insight_300"]) == 300
    assert len(suites["insight_pos_60"]) == 60
    assert len(suites["insight_inventory_60"]) == 60
    assert len(suites["insight_procurement_45"]) == 45
    assert len(suites["insight_audit_45"]) == 45
    assert len(suites["insight_product_45"]) == 45
    assert len(suites["insight_subscription_20"]) == 20
    assert len(suites["insight_host_25"]) == 25


def test_generated_a2a_eval_corpus_metadata_is_complete() -> None:
    module = _load_script_module("a2a_eval_corpus_test_meta", "scripts/a2a_eval_corpus.py")

    scenarios = module.GENERATED_SCENARIOS
    keys = [item["key"] for item in scenarios]
    assert len(keys) == len(set(keys))

    for item in scenarios:
        assert item["area"]
        assert item["agent"]
        assert item["text"]
        assert item["expect_all"]
        assert item["expect_any"]


def test_live_smoke_registers_generated_suite() -> None:
    module = _load_script_module("a2a_live_smoke_test", "scripts/a2a_live_smoke.py")

    assert "insight_300" in module.SCENARIO_SUITES
    assert len(module.SCENARIO_SUITES["insight_300"]) == 300

    scenario_map = {item.key: item for item in module._scenario_matrix()}
    assert "insight_pos_sales_location_01" in scenario_map
    assert scenario_map["insight_pos_sales_location_01"].expect_all == ("insight_response", "metric_grid")
