"""Registry tests use only temporary directories; no real research experiments."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app.experiment_registry import (SCHEMA_VERSION, RegistryConflictError,
                                     register_experiment, record_result)


def manifest():
    return dict(schema_version=SCHEMA_VERSION, experiment_id="momentum-research-v1",
        study_family_id="btc-eth-lead-lag", influenced_by_experiment_ids=[],
        experiment_type="FORECAST_RESEARCH", hypothesis="Импульс превышает ALWAYS_UP на test периоде",
        strategy_id="challenger", strategy_version="v1", code_commit_sha="a"*40,
        config_hash="b"*64, dataset_id="frozen-hourly-v1", dataset_hash="c"*64,
        symbols=["BTCUSDT", "ETHUSDT"],
        development_period=dict(start="2020-01-01", end="2022-01-01"),
        validation_period=dict(start="2022-01-01", end="2023-01-01"),
        final_test_period=dict(start="2023-01-01", end="2024-01-01"),
        evaluator_version="common-evaluator-v1", benchmarks=["ALWAYS_UP", "EMA20_50_1H"],
        primary_endpoint="accuracy_excess_pp_vs_ALWAYS_UP_1h",
        secondary_endpoints=["directional_accuracy_pct_4h"],
        metrics=["directional_accuracy_pct", "coverage_pct", "accuracy_excess_pp"],
        acceptance_criteria=dict(min_accuracy_excess_pp=3., min_coverage_pct=20.),
        notes="Declared research only; no claim that these periods were untouched.")


def result(receipt, status="COMPLETED"):
    return dict(**receipt, completed_at="2026-10-17T12:00:00Z", status=status,
                summary=dict(message="synthetic unit test", accepted=False))


def test_valid_manifest_creates_directory_and_exact_canonical_bytes(tmp_path):
    root = tmp_path/"research"/"experiments"
    declared = manifest()
    before = copy.deepcopy(declared)
    receipt = register_experiment(declared, root)
    path = root/declared["experiment_id"]/"manifest.json"
    raw = path.read_bytes()
    assert raw == json.dumps(declared, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert receipt == dict(experiment_id=declared["experiment_id"], manifest_hash=hashlib.sha256(raw).hexdigest())
    assert "Импульс".encode("utf-8") in raw
    assert json.loads(raw) == before and declared == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["manifest.json", "registration.json"]


def test_canonical_hash_is_stable_across_mapping_order_and_json_formatting(tmp_path):
    original = manifest()
    reformatted = json.loads(json.dumps(original, indent=4, ensure_ascii=True))
    reordered = dict(reversed(list(reformatted.items())))
    reordered["development_period"] = dict(reversed(list(reordered["development_period"].items())))
    assert register_experiment(original, tmp_path/"one") == register_experiment(reordered, tmp_path/"two")


def test_identical_registration_is_idempotent_and_does_not_rewrite_file(tmp_path):
    receipt = register_experiment(manifest(), tmp_path)
    path = tmp_path/receipt["experiment_id"]/"manifest.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert register_experiment(manifest(), tmp_path) == receipt
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("change", [dict(hypothesis="Different hypothesis"),
    dict(primary_endpoint="another endpoint"), dict(benchmarks=["PREVIOUS_HOUR_MOMENTUM"]),
    dict(config_hash="new-config"), dict(code_commit_sha="new-code"), dict(dataset_hash="new-snapshot"),
    dict(final_test_period=dict(start="2023-02-01", end="2024-01-01")),
    dict(acceptance_criteria="Changed acceptance rule")])
def test_changed_manifest_same_id_is_rejected_without_overwrite(tmp_path, change):
    declared = manifest()
    receipt = register_experiment(declared, tmp_path)
    path = tmp_path/receipt["experiment_id"]/"manifest.json"
    raw = path.read_bytes()
    with pytest.raises(RegistryConflictError, match="new experiment_id"):
        register_experiment({**declared, **change}, tmp_path)
    assert path.read_bytes() == raw
    assert len(list(path.parent.iterdir())) == 2


@pytest.mark.parametrize("field,value", [("hypothesis", " "), ("symbols", []), ("benchmarks", []),
    ("strategy_id", ""), ("strategy_version", ""), ("code_commit_sha", ""), ("config_hash", ""),
    ("dataset_id", ""), ("dataset_hash", ""), ("evaluator_version", ""), ("primary_endpoint", ""),
    ("metrics", []), ("acceptance_criteria", {}), ("symbols", "BTCUSDT"), ("benchmarks", [" "]),
    ("symbols", ["BTCUSDT", "BTCUSDT"]), ("schema_version", 2), ("schema_version", True),
    ("experiment_type", "LIVE"), ("secondary_endpoints", "accuracy"), ("notes", {}),
    ("acceptance_criteria", dict(threshold=float("nan")))])
def test_invalid_manifest_is_rejected_before_any_directory_is_created(tmp_path, field, value):
    declared = {**manifest(), field: value}
    root = tmp_path/"registry"
    with pytest.raises(ValueError):
        register_experiment(declared, root)
    assert not root.exists()


@pytest.mark.parametrize("field", ["primary_endpoint", "metrics", "development_period", "secondary_endpoints",
                                  "study_family_id", "influenced_by_experiment_ids"])
def test_missing_required_manifest_fields_are_rejected(tmp_path, field):
    declared = manifest()
    del declared[field]
    with pytest.raises(ValueError, match="Missing manifest fields"):
        register_experiment(declared, tmp_path)


@pytest.mark.parametrize("period,dates", [
    ("development_period", ("2022-01-01", "2020-01-01")),
    ("validation_period", ("2022-01-01", "2022-01-01")),
    ("validation_period", ("2021-12-31", "2023-01-01")),
    ("final_test_period", ("2022-12-31", "2024-01-01")),
    ("final_test_period", ("2018-01-01", "2019-01-01")),
])
def test_invalid_period_order_and_overlap_rejected(tmp_path, period, dates):
    declared = manifest()
    declared[period] = dict(zip(("start", "end"), dates))
    with pytest.raises(ValueError, match="ordered and non-overlapping"):
        register_experiment(declared, tmp_path)


def test_period_comparison_uses_actual_timezones_not_string_order(tmp_path):
    declared = manifest()
    declared["validation_period"]["start"] = "2022-01-01T01:00:00+02:00"
    with pytest.raises(ValueError, match="non-overlapping"):
        register_experiment(declared, tmp_path)
    declared["validation_period"]["start"] = "2022-01-01T02:00:00+02:00"
    register_experiment(declared, tmp_path)  # Exactly adjacent at midnight UTC.


@pytest.mark.parametrize("value", ["yesterday", "2022-02-30", "2022-01-01T00:00:00"])
def test_ambiguous_or_invalid_dates_are_rejected(tmp_path, value):
    declared = manifest()
    declared["validation_period"]["start"] = value
    with pytest.raises(ValueError, match="ISO"):
        register_experiment(declared, tmp_path)


def test_trading_research_and_extra_declared_policies_are_preserved(tmp_path):
    declared = {**manifest(), "experiment_type": "TRADING_RESEARCH", "secondary_endpoints": [],
                "warmup_policy": dict(hours=500, note="Caller-defined, not evaluated by Registry")}
    receipt = register_experiment(declared, tmp_path)
    assert json.loads((tmp_path/receipt["experiment_id"]/"manifest.json").read_bytes()) == declared


def test_result_cannot_be_recorded_without_manifest(tmp_path):
    with pytest.raises(FileNotFoundError):
        record_result("missing", result(dict(experiment_id="missing", manifest_hash="a"*64)), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "ABORTED"])
def test_all_terminal_results_record_and_preserve_manifest(tmp_path, status):
    receipt = register_experiment(manifest(), tmp_path)
    directory = tmp_path/receipt["experiment_id"]
    original = (directory/"manifest.json").read_bytes()
    declared = result(receipt, status)
    before = copy.deepcopy(declared)
    recorded = record_result(receipt["experiment_id"], declared, tmp_path)
    raw = (directory/"result.json").read_bytes()
    assert json.loads(raw) == declared and declared == before
    assert recorded == {**receipt, "result_hash": hashlib.sha256(raw).hexdigest()}
    assert (directory/"manifest.json").read_bytes() == original
    assert sorted(p.name for p in directory.iterdir()) == ["manifest.json", "registration.json", "result.json"]


@pytest.mark.parametrize("change", [dict(manifest_hash="f"*64), dict(experiment_id="another"),
    dict(status="RUNNING"), dict(status="LIVE"), dict(completed_at="not-a-date"), dict(summary=""),
    dict(summary=None), dict(summary={"invalid": float("inf")} )])
def test_invalid_result_identity_status_or_content_is_rejected(tmp_path, change):
    receipt = register_experiment(manifest(), tmp_path)
    with pytest.raises(ValueError):
        record_result(receipt["experiment_id"], {**result(receipt), **change}, tmp_path)
    assert not (tmp_path/receipt["experiment_id"]/"result.json").exists()


@pytest.mark.parametrize("field", ["experiment_id", "manifest_hash", "completed_at", "status", "summary"])
def test_result_requires_all_contract_fields(tmp_path, field):
    receipt = register_experiment(manifest(), tmp_path)
    declared = result(receipt)
    del declared[field]
    with pytest.raises(ValueError, match="Result needs"):
        record_result(receipt["experiment_id"], declared, tmp_path)


def test_identical_result_is_idempotent_changed_result_cannot_replace_failure(tmp_path):
    receipt = register_experiment(manifest(), tmp_path)
    declared = result(receipt, "FAILED")
    first = record_result(receipt["experiment_id"], declared, tmp_path)
    path = tmp_path/receipt["experiment_id"]/"result.json"
    original = (path.read_bytes(), path.stat().st_mtime_ns)
    assert record_result(receipt["experiment_id"], dict(reversed(list(declared.items()))), tmp_path) == first
    with pytest.raises(RegistryConflictError):
        record_result(receipt["experiment_id"], result(receipt, "COMPLETED"), tmp_path)
    with pytest.raises(RegistryConflictError):
        record_result(receipt["experiment_id"], {**declared, "summary": "Revised result"}, tmp_path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


@pytest.mark.parametrize("experiment_id", ["", "..", "../escape", "a/b", "a\\b", "C:escape", "CON", "/absolute"])
def test_experiment_id_cannot_escape_root_or_use_device_alias(tmp_path, experiment_id):
    with pytest.raises(ValueError, match="experiment_id"):
        register_experiment({**manifest(), "experiment_id": experiment_id}, tmp_path/"registry")
    assert list(tmp_path.iterdir()) == []


def test_unrelated_files_are_untouched_and_no_temporary_files_remain(tmp_path):
    unrelated = tmp_path/"unrelated.json"
    unrelated.write_bytes(b"do not modify\n")
    receipt = register_experiment(manifest(), tmp_path)
    extra = tmp_path/receipt["experiment_id"]/"research-note.txt"
    extra.write_text("keep", encoding="utf-8")
    record_result(receipt["experiment_id"], result(receipt), tmp_path)
    assert unrelated.read_bytes() == b"do not modify\n"
    assert extra.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.rglob("*.tmp"))


def test_concurrent_identical_registration_is_atomic_and_idempotent(tmp_path):
    with ThreadPoolExecutor(4) as pool:
        receipts = list(pool.map(lambda _: register_experiment(manifest(), tmp_path), range(4)))
    assert receipts == [receipts[0]]*4
    directory = tmp_path/receipts[0]["experiment_id"]
    assert sorted(p.name for p in directory.iterdir()) == ["manifest.json", "registration.json"]


def test_concurrent_different_results_have_one_immutable_winner(tmp_path):
    receipt = register_experiment(manifest(), tmp_path)
    def attempt(status):
        try:
            return record_result(receipt["experiment_id"], result(receipt, status), tmp_path)
        except RegistryConflictError:
            return None
    with ThreadPoolExecutor(2) as pool:
        receipts = list(pool.map(attempt, ("COMPLETED", "ABORTED")))
    assert sum(r is not None for r in receipts) == 1
    raw = (tmp_path/receipt["experiment_id"]/"result.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == next(r for r in receipts if r)["result_hash"]


@pytest.mark.parametrize("value", ["", "   ", None, 12])
def test_study_family_must_be_nonempty_text(tmp_path, value):
    with pytest.raises(ValueError, match="study_family_id"):
        register_experiment({**manifest(), "study_family_id": value}, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("dependencies", [[], ["previous-v1", "another-prior-experiment"]])
def test_declared_prior_influence_is_preserved_without_requiring_references_on_disk(tmp_path, dependencies):
    declared = {**manifest(), "influenced_by_experiment_ids": dependencies}
    receipt = register_experiment(declared, tmp_path)
    saved = json.loads((tmp_path/receipt["experiment_id"]/"manifest.json").read_bytes())
    assert saved["influenced_by_experiment_ids"] == dependencies
    assert saved["study_family_id"] == declared["study_family_id"]


@pytest.mark.parametrize("dependencies", ["previous-v1", None, [""], ["   "], [12],
                                        ["previous-v1", "previous-v1"], ["momentum-research-v1"]])
def test_invalid_prior_influence_is_rejected(tmp_path, dependencies):
    with pytest.raises(ValueError, match="influenced_by_experiment_ids"):
        register_experiment({**manifest(), "influenced_by_experiment_ids": dependencies}, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", [dict(study_family_id="another-family"),
                                   dict(influenced_by_experiment_ids=["previous-v1"])])
def test_lineage_changes_manifest_hash_and_cannot_replace_registered_manifest(tmp_path, change):
    declared = manifest()
    first = register_experiment(declared, tmp_path/"one")
    second = register_experiment({**declared, **change}, tmp_path/"two")
    assert first["manifest_hash"] != second["manifest_hash"]
    path = tmp_path/"one"/first["experiment_id"]/"registration.json"
    original = path.read_bytes()
    with pytest.raises(RegistryConflictError):
        register_experiment({**declared, **change}, tmp_path/"one")
    assert path.read_bytes() == original


def test_registration_created_with_matching_identity_hash_and_registry_utc_time(tmp_path):
    start = datetime.now(timezone.utc)
    receipt = register_experiment(manifest(), tmp_path)
    end = datetime.now(timezone.utc)
    path = tmp_path/receipt["experiment_id"]/"registration.json"
    metadata = json.loads(path.read_bytes())
    assert set(metadata) == {"experiment_id", "manifest_hash", "registered_at"}
    assert metadata["experiment_id"] == receipt["experiment_id"]
    assert metadata["manifest_hash"] == receipt["manifest_hash"]
    assert metadata["manifest_hash"] == hashlib.sha256((path.parent/"manifest.json").read_bytes()).hexdigest()
    assert metadata["registered_at"].endswith("Z")
    assert start <= datetime.fromisoformat(metadata["registered_at"]) <= end
    assert "registered_at" not in json.loads((path.parent/"manifest.json").read_bytes())


def test_identical_registration_preserves_timestamp_bytes_and_mtime(tmp_path):
    receipt = register_experiment(manifest(), tmp_path)
    path = tmp_path/receipt["experiment_id"]/"registration.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    timestamp = json.loads(before[0])["registered_at"]
    assert register_experiment(manifest(), tmp_path) == receipt
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert json.loads(path.read_bytes())["registered_at"] == timestamp


@pytest.mark.parametrize("change", [dict(experiment_id="wrong-id"), dict(manifest_hash="f"*64),
    dict(registered_at="yesterday"), dict(registered_at="2026-01-01T00:00:00"),
    dict(registered_at="2026-01-01T00:00:00+02:00"), dict(registered_at=12), dict(extra="unexpected")])
def test_registration_metadata_is_not_silently_overwritten(tmp_path, change):
    receipt = register_experiment(manifest(), tmp_path)
    path = tmp_path/receipt["experiment_id"]/"registration.json"
    metadata = {**json.loads(path.read_bytes()), **change}
    path.write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    with pytest.raises(RegistryConflictError, match="registration.json"):
        register_experiment(manifest(), tmp_path)
    with pytest.raises(RegistryConflictError, match="registration.json"):
        record_result(receipt["experiment_id"], result(receipt), tmp_path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not (path.parent/"result.json").exists()


def test_missing_registration_metadata_is_not_recreated_with_a_new_time(tmp_path):
    receipt = register_experiment(manifest(), tmp_path)
    path = tmp_path/receipt["experiment_id"]/"registration.json"
    path.unlink()
    with pytest.raises(FileNotFoundError):
        register_experiment(manifest(), tmp_path)
    assert not path.exists()


def test_failed_new_registration_does_not_publish_partial_experiment(tmp_path, monkeypatch):
    import app.experiment_registry as registry
    original = registry._store_once
    def fail_metadata(path, raw):
        if path.name == "registration.json":
            raise OSError("simulated storage failure")
        return original(path, raw)
    monkeypatch.setattr(registry, "_store_once", fail_metadata)
    with pytest.raises(OSError, match="simulated storage failure"):
        register_experiment(manifest(), tmp_path)
    assert list(tmp_path.iterdir()) == []
