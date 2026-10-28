"""Phase 3 — inbound signals, deterministic proposals and review."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trajectory_os.mvp import model, proposals, signals, store


def _seed(root: object) -> None:
    store.save_portfolio(root, model.Portfolio(
        schema_version=model.SCHEMA_VERSION, name="phase3",
        projects=(model.Project(
            project_id="p1", name="Donation maison et terrains",
            objective="finaliser le dossier"),),
        tasks=(
            model.Task(task_id="t1", project_id="p1",
                       title="Finaliser les attestations",
                       status=model.TS_WAITING,
                       waiting_for=("notaire",)),
        ),
    ))


def _now() -> datetime:
    return datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def test_signal_analysis_and_acceptance(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    bundle = signals.create_signal(
        root, text="Le notaire a envoyé les pièces. Échéance 15/11/2026.",
        source_ref="mail-notaire", project_hint="p1", now=_now())
    ops = {item["op"] for item in bundle["proposals"]}
    assert signals.OP_RESOLVE_WAITING in ops
    assert signals.OP_SET_DEADLINE in ops
    assert bundle["signal"]["status"] == signals.STATUS_ANALYZED

    waiting = next(item for item in bundle["proposals"]
                   if item["op"] == signals.OP_RESOLVE_WAITING)
    proposals.apply_proposal(root, bundle["signal"]["signal_id"],
                             waiting["proposal_id"])
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.task_map()["t1"].status == model.TS_TODO
    assert portfolio.task_map()["t1"].waiting_for == ()

    deadline = next(item for item in bundle["proposals"]
                    if item["op"] == signals.OP_SET_DEADLINE)
    applied = proposals.apply_proposal(
        root, bundle["signal"]["signal_id"], deadline["proposal_id"])
    assert applied["signal_status"] == signals.STATUS_APPLIED
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    assert portfolio.project_map()["p1"].deadline == "2026-11-15"

    evidence = signals.evidence_path(root).read_text(encoding="utf-8")
    assert "ACCEPTED" in evidence
    assert "mail-notaire" in evidence


def test_signal_duplicate_and_mute(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    first = signals.create_signal(root, text="appeler le notaire",
                                  source_ref="mail", now=_now())
    second = signals.create_signal(root, text="appeler le notaire",
                                   source_ref="mail", now=_now())
    assert second["duplicate"] is True
    assert second["signal"]["signal_id"] == first["signal"]["signal_id"]
    signals.mute_source(root, "mail")
    with pytest.raises(signals.SignalError):
        signals.create_signal(root, text="autre chose", source_ref="mail")


def test_signal_creates_a_task_from_an_action_line(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    bundle = signals.create_signal(
        root,
        text=("Donation maison et terrains : appeler le notaire demain. "
              "Prévoir 30 min."),
        source_ref="mail", now=_now())
    create = next(item for item in bundle["proposals"]
                  if item["op"] == signals.OP_CREATE_TASK)
    assert create["after"]["project_id"] == "p1"
    assert create["after"]["estimated_minutes"] == 30
    result = proposals.apply_proposal(
        root, bundle["signal"]["signal_id"], create["proposal_id"])
    assert result["result"]["task_id"]
    portfolio = store.load_portfolio(root)
    assert portfolio is not None
    created = portfolio.task_map()[result["result"]["task_id"]]
    assert created.title.startswith("appeler le notaire")
    assert created.estimated_minutes == 30
    assert created.deadline == "2026-10-29"


def test_reject_and_defer_do_not_touch_the_portfolio(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    bundle = signals.create_signal(
        root, text="Le notaire a envoyé les pièces.",
        source_ref="mail", project_hint="p1", now=_now())
    waiting = next(item for item in bundle["proposals"]
                   if item["op"] == signals.OP_RESOLVE_WAITING)
    before = store.default_portfolio_path(root).read_bytes()
    rejected = proposals.reject_proposal(
        root, bundle["signal"]["signal_id"], waiting["proposal_id"])
    assert rejected["proposal"]["state"] == signals.STATE_REJECTED
    assert store.default_portfolio_path(root).read_bytes() == before

    bundle2 = signals.create_signal(
        root, text="Le notaire a envoyé les pièces.",
        source_ref="mail2", project_hint="p1", now=_now())
    waiting2 = next(item for item in bundle2["proposals"]
                    if item["op"] == signals.OP_RESOLVE_WAITING)
    deferred = proposals.defer_proposal(
        root, bundle2["signal"]["signal_id"], waiting2["proposal_id"])
    assert deferred["proposal"]["state"] == signals.STATE_DEFERRED
    assert store.default_portfolio_path(root).read_bytes() == before


def test_signal_endpoints(tmp_path: object) -> None:
    from test_mvp_dashboard_api import _request, _server

    root = str(tmp_path)
    _seed(root)
    with _server(root) as port:
        status, created = _request(port, "POST", "/api/signals", {
            "text": "Le notaire a envoyé les pièces.",
            "source_ref": "mail", "project_hint": "p1",
        })
        assert status == 200
        signal_id = created["signal"]["signal_id"]
        assert created["proposals"]
        status, listing = _request(port, "GET", "/api/signals")
        assert status == 200
        assert listing["signals"]
        proposal = next(item for item in created["proposals"]
                        if item["op"] == signals.OP_RESOLVE_WAITING)
        status, accepted = _request(
            port, "POST",
            f"/api/signals/{signal_id}/proposals/"
            f"{proposal['proposal_id']}/accept", {})
        assert status == 200
        assert accepted["proposal"]["state"] == signals.STATE_ACCEPTED
        status, muted = _request(
            port, "POST", f"/api/signals/{signal_id}/mute",
            {"source_ref": "mail"})
        assert status == 200
        assert "mail" in muted["muted_sources"]


def test_signals_view_is_wired() -> None:
    from trajectory_os.mvp import cockpit_ui, uistate

    assert "signals" in uistate.VIEWS
    js = cockpit_ui.PAGE_JS
    for marker in ("signals:renderSignals", "function renderSignals",
                   "function analyzeSignal", "function acceptProposal",
                   "function rejectProposal", "function deferProposal",
                   "function muteSignalSource"):
        assert marker in js, marker


def test_french_date_and_compound_duration(tmp_path: object) -> None:
    root = str(tmp_path)
    _seed(root)
    bundle = signals.create_signal(
        root,
        text=("Finaliser les attestations : prévoir 1h30. "
              "Échéance 15 novembre 2026."),
        source_ref="mail", project_hint="p1", now=_now())
    deadline = next(item for item in bundle["proposals"]
                    if item["op"] == signals.OP_SET_DEADLINE)
    effort = next(item for item in bundle["proposals"]
                  if item["op"] == signals.OP_SET_EFFORT)
    assert deadline["after"]["deadline"] == "2026-11-15"
    assert effort["after"]["estimated_minutes"] == 90


def test_search_finds_inbound_signals(tmp_path: object) -> None:
    from trajectory_os.mvp import search

    root = str(tmp_path)
    _seed(root)
    signals.create_signal(root, text="Le notaire a envoyé les pièces.",
                          source_ref="mail", project_hint="p1", now=_now())
    result = search.search(root, "notaire")
    assert any(item["kind"] == "signal" for item in result["results"])
