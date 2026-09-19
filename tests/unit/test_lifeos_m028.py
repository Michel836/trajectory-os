"""M028 — LifeOS integration unit tests (no Git, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_os.lifeos import adapters, engine, model, store, summary


def _seed_goal(root: Path) -> str:
    from trajectory_os.goals import launch
    from trajectory_os.graph import store as graph_store

    goal_id = "g-lifeos-unit"
    spec = {
        "schema_version": 1,
        "goal_id": goal_id,
        "objective": "Prove scoped LifeOS exchange.",
        "nodes": [{
            "node_id": "n-lifeos",
            "title": "LifeOS mission",
            "priority": 50,
            "depends_on": [],
            "acceptance_criteria": [{
                "criterion_id": "ac-lifeos",
                "statement": "exchange is scoped and idempotent"}],
            "mission_ref": {"mission_id": "m-lifeos-unit", "required": True},
            "resources": {"cpu_slots": 1},
            "budgets": {"repair_budget": 0, "max_attempts": 1},
        }],
    }
    defaults = launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)
    launch.provision_from_spec(str(root), spec, defaults)
    graph_store.create_graph(str(root), spec, repo_root=defaults.repo_root,
                             baseline_revision=defaults.baseline_revision)
    return goal_id


def _config(target: Path) -> model.LifeOSConfig:
    return model.LifeOSConfig(adapters=(
        model.AdapterConfig(kind=model.AK_OBSIDIAN,
                            target=str(target / "vault")),
        model.AdapterConfig(kind=model.AK_SUPER_PRODUCTIVITY,
                            target=str(target / "sp")),
        model.AdapterConfig(kind=model.AK_JSON_MANIFEST,
                            target=str(target / "json")),
    )).validate()


def test_config_validation_and_disable() -> None:
    with pytest.raises(model.LifeOSError):
        model.LifeOSConfig(adapters=(
            model.AdapterConfig(kind="nope", target="/tmp"),)).validate()
    with pytest.raises(model.LifeOSError):
        model.LifeOSConfig(adapters=(
            model.AdapterConfig(kind=model.AK_OBSIDIAN),)).validate()
    with pytest.raises(model.LifeOSError):
        model.LifeOSConfig(adapters=(
            model.AdapterConfig(kind=model.AK_OBSIDIAN, target="/a"),
            model.AdapterConfig(kind=model.AK_OBSIDIAN, target="/b"),
        )).validate()
    disabled = model.LifeOSConfig(adapters=(
        model.AdapterConfig(kind=model.AK_OBSIDIAN, enabled=False),))
    assert disabled.validate().enabled == ()


def test_exchange_is_scoped_idempotent_and_provenanced(
        tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    target = tmp_path / "lifeos-out"
    report = engine.run_exchange(root, goal_id, _config(target),
                                 clock=lambda: "2026-01-01T00:00:00Z")
    assert report.status == "OK"
    assert report.exchange_id == report.to_dict()["exchange_id"]
    assert all(record.status == model.XS_OK for record in report.records)
    assert all(record.outputs for record in report.records)
    assert report.canonical_unchanged is True

    # Every output is content-addressed and actually written.
    for record in report.records:
        for output in record.outputs:
            path = Path(output.path)
            assert path.is_file()
            assert output.size_bytes == path.stat().st_size

    # A second exchange over unchanged state is an idempotent no-op.
    again = engine.run_exchange(root, goal_id, _config(target),
                                clock=lambda: "2026-01-01T00:00:01Z")
    assert again.status == "UNCHANGED"
    assert again.exchange_id == report.exchange_id
    assert all(record.status == model.XS_SKIPPED for record in again.records)

    count, histogram = store.reconstruct(root)
    assert count == 3 and histogram.get(model.XS_OK) == 3


def test_target_inside_root_fails_isolated(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    config = model.LifeOSConfig(adapters=(
        model.AdapterConfig(kind=model.AK_OBSIDIAN,
                            target=str(root / "leak")),
        model.AdapterConfig(kind=model.AK_JSON_MANIFEST,
                            target=str(tmp_path / "ok")),
    )).validate()
    report = engine.run_exchange(root, goal_id, config,
                                 clock=lambda: "2026-01-01T00:00:00Z")
    statuses = {record.adapter: record for record in report.records}
    assert statuses[model.AK_OBSIDIAN].status == model.XS_FAILED
    assert statuses[model.AK_OBSIDIAN].error == model.E_TARGET_INSIDE_ROOT
    assert statuses[model.AK_JSON_MANIFEST].status == model.XS_OK
    assert report.status == "PARTIAL"
    assert report.canonical_unchanged is True
    assert not (root / "leak").exists()


def test_adapter_exception_is_isolated(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    # A target path that is a regular file makes mkdir fail inside the
    # adapter; the failure is captured, not propagated.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    config = model.LifeOSConfig(adapters=(
        model.AdapterConfig(kind=model.AK_OBSIDIAN, target=str(blocker)),
        model.AdapterConfig(kind=model.AK_JSON_MANIFEST,
                            target=str(tmp_path / "ok")),
    )).validate()
    report = engine.run_exchange(root, goal_id, config,
                                 clock=lambda: "2026-01-01T00:00:00Z")
    statuses = {record.adapter: record for record in report.records}
    assert statuses[model.AK_OBSIDIAN].status == model.XS_FAILED
    assert statuses[model.AK_OBSIDIAN].error
    assert statuses[model.AK_JSON_MANIFEST].status == model.XS_OK
    assert report.canonical_unchanged is True


def test_obsidian_and_super_productivity_payloads(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    target = tmp_path / "out"
    engine.run_exchange(root, goal_id, _config(target),
                        clock=lambda: "2026-01-01T00:00:00Z")
    note = target / "vault" / "TrajectoryOS" / f"{goal_id}.md"
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    assert "trajectory_goal:" in text
    sp = json.loads((target / "sp" / f"trajectory-os-{goal_id}.json")
                    .read_text(encoding="utf-8"))
    assert sp["trajectory_os"]["goal_id"] == goal_id
    manifest = json.loads(
        (target / "json" / f"{goal_id}.lifeos.json").read_text("utf-8"))
    assert manifest["exchange_id"]
    assert manifest["snapshot"]["goal_id"] == goal_id


def test_scope_limits_events(tmp_path: Path) -> None:
    config = model.LifeOSConfig(
        scope=model.ExchangeScope(event_categories=("PROOF",),
                                  include_artifacts=False),
        adapters=(model.AdapterConfig(kind=model.AK_JSON_MANIFEST,
                                      target=str(tmp_path / "json")),),
    ).validate()
    assert config.scope.identity_payload()["include_artifacts"] is False


def test_ledger_bounded_and_reconstruct(tmp_path: Path) -> None:
    records = [
        model.ExchangeRecord.build(
            adapter=model.AK_JSON_MANIFEST, exchange_id=f"e{i}",
            goal_id="g", status=model.XS_OK, event_ids=(), artifact_ids=(),
            outputs=(), created_at="2026-01-01T00:00:00Z")
        for i in range(model.MAX_RECORDS + 5)
    ]
    store.save_ledger(tmp_path, records)
    count, histogram = store.reconstruct(tmp_path)
    assert count == model.MAX_RECORDS
    assert histogram == {model.XS_OK: model.MAX_RECORDS}


def test_summary_and_no_git_write(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    engine.run_exchange(root, goal_id, _config(tmp_path / "out"),
                        clock=lambda: "2026-01-01T00:00:00Z")
    document = summary.status_document(root)
    assert document["status"] == "OK"
    assert "lifeos" in summary.render(document)
    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")
    source = "".join(Path(module.__file__).read_text(encoding="utf-8")
                     for module in (engine, model, adapters, store, summary))
    assert not [verb for verb in verbs
                if f'"git", "{verb}"' in source
                or f"'git', '{verb}'" in source]
