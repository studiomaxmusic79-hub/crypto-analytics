"""Minimal file-based RESEARCH registration, independent of LIVE/backtest.

Pass research/experiments as root_dir; each safe experiment_id gets manifest.json
and registration.json, then later result.json. Registration time is a local UTC
audit timestamp, not cryptographic time proof or evidence of unseen periods.
This module never inspects results to decide whether research was honest. Declarations and
extra JSON fields (e.g. warmup policy) are preserved, not interpreted.

Manifest v1 requires the fields checked below. Periods are half-open [start,end):
ISO dates mean midnight UTC; timestamps require an explicit timezone. Adjacent
periods are allowed. Timestamp strings remain as declared in canonical JSON.

Immutability is enforced by this API, not against out-of-band filesystem edits or
as independent proof of preregistration time. Atomic creation requires a local
filesystem supporting hard links (e.g. NTFS); there is no unsafe overwrite fallback.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import date, datetime, timezone

SCHEMA_VERSION = 1
EXPERIMENT_TYPES = ("FORECAST_RESEARCH", "TRADING_RESEARCH")
RESULT_STATUSES = ("COMPLETED", "FAILED", "ABORTED")
PERIODS = ("development_period", "validation_period", "final_test_period")


class RegistryConflictError(ValueError):
    """An immutable registry file already contains different exact bytes."""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


def _experiment_id(value):
    # A single portable directory name; no path traversal, Windows aliases or ADS.
    if (not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None
        or value.upper() in {"CON", "PRN", "AUX", "NUL",
                             *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}):
        raise ValueError("experiment_id must be a safe directory name (letters, digits, _ or -)")


def _time(value, field):
    _text(value, field)
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timestamp needs an explicit timezone")
        return parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date or timezone-aware ISO timestamp") from exc


def _strings(value, field, *, allow_empty=False):
    if not isinstance(value, list) or not allow_empty and not value:
        raise ValueError(f"{field} must be a {'possibly empty' if allow_empty else 'nonempty'} list")
    for item in value:
        _text(item, field)
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicates")


def _description(value, field):
    if isinstance(value, str):
        _text(value, field)
    elif not isinstance(value, dict) or not value:
        raise ValueError(f"{field} must be nonempty text or a nonempty JSON object")


def _canonical_bytes(value):
    def keys(item):
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                keys(child)
        elif isinstance(item, list):
            for child in item:
                keys(child)
    keys(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Registry content must be finite UTF-8 JSON data") from exc


def _validate_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    required = {"schema_version", "experiment_id", "experiment_type", "hypothesis", "strategy_id",
                "strategy_version", "code_commit_sha", "config_hash", "dataset_id", "dataset_hash",
                "symbols", *PERIODS, "evaluator_version", "benchmarks", "primary_endpoint",
                "secondary_endpoints", "metrics", "acceptance_criteria", "study_family_id",
                "influenced_by_experiment_ids"}
    if required - manifest.keys():
        raise ValueError("Missing manifest fields: " + ", ".join(sorted(required - manifest.keys())))
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported manifest schema_version")
    _experiment_id(manifest["experiment_id"])
    if manifest["experiment_type"] not in EXPERIMENT_TYPES:
        raise ValueError("Only FORECAST_RESEARCH and TRADING_RESEARCH are supported; no LIVE")
    for field in ("hypothesis", "strategy_id", "strategy_version", "code_commit_sha", "config_hash",
                  "dataset_id", "dataset_hash", "evaluator_version", "primary_endpoint", "study_family_id"):
        _text(manifest[field], field)
    for field in ("symbols", "benchmarks", "metrics"):
        _strings(manifest[field], field)
    _strings(manifest["secondary_endpoints"], "secondary_endpoints", allow_empty=True)
    _strings(manifest["influenced_by_experiment_ids"], "influenced_by_experiment_ids", allow_empty=True)
    if manifest["experiment_id"] in manifest["influenced_by_experiment_ids"]:
        raise ValueError("influenced_by_experiment_ids must not reference the experiment itself")
    _description(manifest["acceptance_criteria"], "acceptance_criteria")
    if "notes" in manifest and not isinstance(manifest["notes"], str):
        raise ValueError("notes must be a string")
    previous_end = None
    for field in PERIODS:
        period = manifest[field]
        if not isinstance(period, dict) or not {"start", "end"} <= period.keys():
            raise ValueError(f"{field} needs start and end")
        start, end = (_time(period[k], f"{field}.{k}") for k in ("start", "end"))
        if start >= end or previous_end is not None and start < previous_end:
            raise ValueError("Research periods must be ordered and non-overlapping with start < end")
        previous_end = end


def _directory(experiment_id, root_dir):
    _experiment_id(experiment_id)
    root = Path(root_dir).resolve()
    directory = root / experiment_id
    if directory.is_symlink() or directory.resolve().parent != root:
        raise ValueError("Experiment directory must remain inside root_dir without symlink aliases")
    return directory


def _read(path):
    if path.is_symlink():
        raise ValueError("Registry files must not be symlinks")
    return path.read_bytes()


def _store_once(path, raw):
    try:
        existing = _read(path)
    except FileNotFoundError:
        existing = None
    if existing is None:
        # Publish a complete file atomically, without overwriting an existing name.
        # Concurrent identical registrations see complete bytes, never a partial write.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".registry-", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            existing = _read(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if existing != raw:
        raise RegistryConflictError(f"{path.name} already differs; use a new experiment_id")
    return hashlib.sha256(existing).hexdigest()


def _registration(directory, experiment_id, manifest_hash):
    raw = _read(directory / "registration.json")
    try:
        metadata = json.loads(raw)
        if (not isinstance(metadata, dict)
            or set(metadata) != {"experiment_id", "manifest_hash", "registered_at"}
            or metadata["experiment_id"] != experiment_id
            or metadata["manifest_hash"] != manifest_hash
            or not isinstance(metadata["registered_at"], str)
            or not metadata["registered_at"].endswith("Z")
            or "T" not in metadata["registered_at"]):
            raise ValueError("Invalid registration identity or UTC timestamp")
        _time(metadata["registered_at"], "registered_at")
        if _canonical_bytes(metadata) != raw:
            raise ValueError("Registration metadata must be canonical")
    except (ValueError, TypeError) as exc:
        raise RegistryConflictError("registration.json is invalid or differs; refusing to overwrite") from exc


def register_experiment(manifest, root_dir):
    """Validate, canonicalize and create <root>/<id>/manifest.json once.

    config_hash/dataset_hash/code_commit_sha are nonempty caller-supplied identity
    references; this registry does not verify external code, configs or datasets.
    study_family_id groups related hypotheses; influenced_by_experiment_ids declares
    prior influence without checking referenced experiments or discovering omissions.
    A new directory publishes manifest and Registry-generated UTC metadata together.
    Missing/corrupt metadata on an existing experiment fails closed, never recreates
    a timestamp. Immutability does not protect against out-of-band filesystem edits.
    """
    _validate_manifest(manifest)
    raw = _canonical_bytes(manifest)
    experiment_id = manifest["experiment_id"]
    directory = _directory(experiment_id, root_dir)
    digest = hashlib.sha256(raw).hexdigest()
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".registry-") as temporary:
            staging = Path(temporary)
            _store_once(staging / "manifest.json", raw)
            metadata = dict(experiment_id=experiment_id, manifest_hash=digest,
                            registered_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            _store_once(staging / "registration.json", _canonical_bytes(metadata))
            try:
                os.rename(staging, directory)
            except OSError:
                if not directory.is_dir():
                    raise
                # A concurrent registration may have published its complete directory.
    if _read(directory / "manifest.json") != raw:
        raise RegistryConflictError("manifest.json already differs; use a new experiment_id")
    _registration(directory, experiment_id, digest)
    return dict(experiment_id=experiment_id, manifest_hash=digest)


def record_result(experiment_id, result, root_dir):
    """Bind one immutable terminal result (including FAILED/ABORTED) to a manifest.

    Required: experiment_id, manifest_hash, completed_at, status, summary (nonempty
    text or JSON object). Extra result fields are preserved without a metrics schema.
    """
    directory = _directory(experiment_id, root_dir)
    manifest_raw = _read(directory / "manifest.json")  # Missing manifest: no directories are created.
    manifest = json.loads(manifest_raw)
    _validate_manifest(manifest)
    if manifest["experiment_id"] != experiment_id or _canonical_bytes(manifest) != manifest_raw:
        raise ValueError("Stored manifest must be canonical and match the experiment directory")
    manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
    _registration(directory, experiment_id, manifest_hash)
    if not isinstance(result, dict) or not {"experiment_id", "manifest_hash", "completed_at", "status", "summary"} <= result.keys():
        raise ValueError("Result needs experiment_id, manifest_hash, completed_at, status and summary")
    if result["experiment_id"] != experiment_id or result["manifest_hash"] != manifest_hash:
        raise ValueError("Result experiment_id/manifest_hash must match the registered manifest")
    if result["status"] not in RESULT_STATUSES:
        raise ValueError("Result status must be COMPLETED, FAILED or ABORTED")
    _time(result["completed_at"], "completed_at")
    _description(result["summary"], "summary")
    digest = _store_once(directory / "result.json", _canonical_bytes(result))
    return dict(experiment_id=experiment_id, manifest_hash=manifest_hash, result_hash=digest)
