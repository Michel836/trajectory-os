"""M064–M071 — practical CLI surface (``scripts/trajectory realworld`` / ``demo``).

Observation commands are read-only. The ``demo portfolio`` command runs a
fully reproducible, privacy-safe demonstration using fixture/sample data. No
command here performs a Git release write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from trajectory_os.intelligence import model as intel_model
from trajectory_os.realworld import (
    acceptance as acceptance_module,
)
from trajectory_os.realworld import (
    career as career_module,
)
from trajectory_os.realworld import (
    cockpit as cockpit_module,
)
from trajectory_os.realworld import (
    dogfood as dogfood_module,
)
from trajectory_os.realworld import (
    ingest as ingest_module,
)
from trajectory_os.realworld import (
    learning as learning_module,
)
from trajectory_os.realworld import (
    lifeos_ops as operations_module,
)
from trajectory_os.realworld import (
    lifesci as lifesci_module,
)
from trajectory_os.realworld import (
    outcomes as outcomes_module,
)
from trajectory_os.realworld import (
    portfolio as portfolio_module,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3

DEFAULT_DEMO_ROOT = ".artifacts/m064-m071/demo"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _emit(args: argparse.Namespace, payload: Any, text: str) -> int:
    if getattr(args, "json", False):
        _print_json(payload)
    else:
        print(text)
    return EXIT_OK


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


# --- realworld subcommands ----------------------------------------------------


def _cmd_ingest(args: argparse.Namespace) -> int:
    manifest = ingest_module.ingest_paths(
        args.path, root=args.root, project_id=args.project_id,
        mission_id=args.mission_id)
    return _emit(args, manifest.to_dict(),
                 f"ingested {manifest.source_count} source(s): "
                 + ", ".join(f"{k}={v}"
                             for k, v in manifest.status_counts().items()))


def _evidence_sources(paths: Any) -> tuple[career_module.EvidenceSource, ...]:
    """Load caller-supplied files as typed career evidence sources."""
    return tuple(
        career_module.EvidenceSource(path, _read_text(path))
        for path in (paths or ()))


def _cmd_career(args: argparse.Namespace) -> int:
    brief = _read_text(args.brief_file) if args.brief_file else ""
    # Each evidence class is supplied and labelled separately. The legacy
    # ``--evidence`` flag is a documented alias for ``--company-evidence``
    # only: it never carries profile, portfolio or research material.
    company_paths = (tuple(args.company_evidence or ())
                     + tuple(getattr(args, "evidence", None) or ()))
    result = career_module.run_career_intelligence(
        career_module.CareerInputs(
            company=args.company, role=args.role,
            role_description=brief,
            company_evidence=_evidence_sources(company_paths),
            profile_evidence=_evidence_sources(args.profile_evidence),
            portfolio_artifacts=_evidence_sources(args.portfolio_artifact),
            research_sources=_evidence_sources(args.research_source),
            inputs_are_fixture=bool(args.sample)),
        root=args.root, workflow_id=args.workflow_id)
    return _emit(args, result.to_dict(),
                 f"career artifacts: {', '.join(sorted(result.artifacts))}")


def _cmd_life_sciences(args: argparse.Namespace) -> int:
    records = tuple(
        lifesci_module.MonitoringRecord(
            str(path), args.date, _read_text(str(path)))
        for path in args.record)
    result = lifesci_module.run_life_sciences_intelligence(
        records, root=args.root, workflow_id=args.workflow_id,
        inputs_are_fixture=bool(args.sample))
    return _emit(args, result.to_dict(),
                 f"life-sciences artifacts: "
                 f"{', '.join(sorted(result.artifacts))}")


def _cmd_operations(args: argparse.Namespace) -> int:
    report = operations_module.evaluate_project_operations(
        args.root, args.project_id, workflow_id=args.workflow_id)
    return _emit(args, report.to_dict(),
                 report.render_markdown())


def _cmd_outcomes(args: argparse.Namespace) -> int:
    summary = outcomes_module.reconciliation_summary(args.root)
    return _emit(args, summary, json.dumps(summary, indent=2, sort_keys=True))


def _cmd_refresh(args: argparse.Namespace) -> int:
    from trajectory_os.intelligence import dataset as dataset_module

    learning_dataset = dataset_module.load_dataset(args.root)
    if learning_dataset is None:
        print("error: no persisted learning dataset at this root",
              file=sys.stderr)
        return EXIT_REJECTED
    report = learning_module.refresh_model(
        learning_dataset, args.target)
    learning_module.persist_report(args.root, report)
    return _emit(args, report.to_dict(), report.render_markdown())


def _cmd_cockpit(args: argparse.Namespace) -> int:
    view = cockpit_module.build_cockpit(
        args.root, (), project_ids=(), workflow_id="cockpit")
    return _emit(args, view.to_dict(), view.render_text())


def _cmd_dogfood(args: argparse.Namespace) -> int:
    evidence = dogfood_module.run_dogfood(
        args.root, real_runs_dir=(args.real_runs_dir or None),
        docs_dir=args.docs_dir)
    return _emit(args, evidence.to_dict(), evidence.render_markdown())


def _cmd_acceptance(args: argparse.Namespace) -> int:
    matrix = acceptance_module.run_acceptance(
        str(Path(args.root) / "acceptance"))
    out = Path(args.out or Path(args.root) / "acceptance" / "matrix.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(matrix.to_dict(), indent=2, sort_keys=True)
                   + "\n", encoding="utf-8")
    text = (f"acceptance cases: {matrix.passed}/{matrix.total} passed\n"
            f"artifact: {out}")
    return _emit(args, matrix.to_dict(), text)


def _cmd_portfolio(args: argparse.Namespace) -> int:
    evidence = dogfood_module.run_privacy_safe_demo(
        args.root, run_acceptance_matrix=not args.no_acceptance)
    portfolio_evidence = _portfolio_of(evidence)
    written = portfolio_module.write_portfolio(args.root, portfolio_evidence)
    text = (f"portfolio documents written under {args.root}:\n"
            + "\n".join(f"  {name}" for name in sorted(written)))
    return _emit(args, {"documents": sorted(written)}, text)


# --- demo ---------------------------------------------------------------------


def _cmd_demo_portfolio(args: argparse.Namespace) -> int:
    root = args.root or DEFAULT_DEMO_ROOT
    evidence = dogfood_module.run_privacy_safe_demo(
        root, run_acceptance_matrix=not args.no_acceptance)
    portfolio_evidence = _portfolio_of(evidence)
    written = portfolio_module.write_portfolio(root, portfolio_evidence)
    overview = portfolio_module.executive_overview(portfolio_evidence)
    if args.json:
        _print_json({"evidence": evidence.to_dict(),
                     "documents": sorted(written)})
        return EXIT_OK
    print(evidence.render_markdown())
    print(overview)
    print("portfolio documents:")
    for name in sorted(written):
        print(f"  {written[name]}")
    return EXIT_OK


def _portfolio_of(evidence: dogfood_module.DogfoodEvidence
                  ) -> portfolio_module.PortfolioEvidence:
    # Rebuild the typed evidence object from the persisted document so the
    # demo can render the executive overview without re-running the pipeline.
    document = evidence.portfolio.get("evidence", {})
    studies = tuple(
        portfolio_module.CaseStudy(
            case_id=str(item.get("case_id", "")),
            title=str(item.get("title", "")),
            problem=str(item.get("problem", "")),
            inputs=tuple(str(x) for x in item.get("inputs", [])),
            approach=tuple(str(x) for x in item.get("approach", [])),
            architecture=tuple(str(x) for x in item.get("architecture", [])),
            evidence=tuple(str(x) for x in item.get("evidence", [])),
            result=tuple(str(x) for x in item.get("result", [])),
            limitations=tuple(str(x) for x in item.get("limitations", [])),
            lessons=tuple(str(x) for x in item.get("lessons", [])))
        for item in document.get("case_studies", []))
    acceptance_doc = evidence.acceptance
    return portfolio_module.PortfolioEvidence(
        generated_at=str(document.get("generated_at", evidence.generated_at)),
        version=str(document.get("version", "")),
        acceptance_passed=int(acceptance_doc.get("passed", 0)),
        acceptance_total=int(acceptance_doc.get("total", 0)),
        dogfood_history_source=str(document.get("dogfood", {}).get(
            "history_source", evidence.history_source)),
        dogfood_real_rows=int(document.get("dogfood", {}).get(
            "real_rows", evidence.real_row_count)),
        workflow_families=tuple(
            str(x) for x in document.get("dogfood", {}).get(
                "workflow_families", [])),
        outcome_links=int(document.get("outcomes", {}).get("links", 0)),
        outcome_reconciled=int(document.get("outcomes", {}).get(
            "reconciled", 0)),
        unresolved_unknowns=int(document.get("unknowns", 0)),
        corrections_required=int(document.get("corrections_required", 0)),
        prediction_error=document.get("outcomes", {}).get("prediction_error"),
        trust_clean=bool(document.get("trust", {}).get("clean", False)),
        trust_scanned_files=int(document.get("trust", {}).get(
            "scanned_files", 0)),
        case_studies=studies,
        source_coverage=int(document.get("source_coverage", 0)),
        output_completeness=float(document.get("output_completeness", 0.0)),
        time_to_deliverable_s=document.get("time_to_deliverable_s"),
        limitations=tuple(str(x) for x in document.get("limitations", [])))


# --- registration -------------------------------------------------------------


def register(sub: Any) -> None:
    realworld = sub.add_parser(
        "realworld", help="M064-M071 real-world operating system")
    rw = realworld.add_subparsers(dest="realworld_command", required=True)

    ingest = rw.add_parser("ingest", help="ingest real inputs")
    ingest.add_argument("--root", required=True)
    ingest.add_argument("--path", action="append", required=True)
    ingest.add_argument("--project-id", dest="project_id", default=None)
    ingest.add_argument("--mission-id", dest="mission_id", default=None)
    ingest.add_argument("--json", action="store_true")

    career = rw.add_parser("career", help="career intelligence workflow")
    career.add_argument("--root", required=True)
    career.add_argument("--company", required=True)
    career.add_argument("--role", required=True)
    career.add_argument("--brief-file", dest="brief_file", default=None)
    career.add_argument(
        "--company-evidence", dest="company_evidence", action="append",
        default=None, metavar="FILE",
        help="external company/role evidence document (repeatable; "
             "emitted as SUPPLIED_EVIDENCE)")
    career.add_argument(
        "--profile-evidence", dest="profile_evidence", action="append",
        default=None, metavar="FILE",
        help="user-supplied CV/profile evidence (repeatable; emitted as "
             "USER_INPUT/profile:* and usable for role fit)")
    career.add_argument(
        "--portfolio-artifact", dest="portfolio_artifact", action="append",
        default=None, metavar="FILE",
        help="user-supplied portfolio artifact (repeatable; emitted as "
             "USER_INPUT/portfolio:* and usable for role fit)")
    career.add_argument(
        "--research-source", dest="research_source", action="append",
        default=None, metavar="FILE",
        help="external research source (repeatable; emitted as "
             "SUPPLIED_EVIDENCE)")
    career.add_argument(
        "--evidence", action="append", default=None, metavar="FILE",
        help="DEPRECATED alias for --company-evidence only; it never "
             "labels profile/portfolio/research material")
    career.add_argument("--workflow-id", dest="workflow_id",
                        default="career")
    career.add_argument("--sample", action="store_true")
    career.add_argument("--json", action="store_true")

    life = rw.add_parser("life-sciences",
                         help="life-sciences intelligence workflow")
    life.add_argument("--root", required=True)
    life.add_argument("--record", action="append", required=True)
    life.add_argument("--date", default="2026-01-01")
    life.add_argument("--workflow-id", dest="workflow_id",
                      default="life-sciences")
    life.add_argument("--sample", action="store_true")
    life.add_argument("--json", action="store_true")

    operations = rw.add_parser("operations", help="LifeOS operational view")
    operations.add_argument("--root", required=True)
    operations.add_argument("--project-id", dest="project_id", required=True)
    operations.add_argument("--workflow-id", dest="workflow_id",
                            default="operations")
    operations.add_argument("--json", action="store_true")

    out = rw.add_parser("outcomes", help="outcome reconciliation summary")
    out.add_argument("--root", required=True)
    out.add_argument("--json", action="store_true")

    refresh = rw.add_parser("refresh", help="champion/challenger refresh")
    refresh.add_argument("--root", required=True)
    refresh.add_argument("--target", default="success")
    refresh.add_argument("--json", action="store_true")

    cockpit = rw.add_parser("cockpit", help="read-only decision cockpit")
    cockpit.add_argument("--root", required=True)
    cockpit.add_argument("--json", action="store_true")

    rw_dogfood = rw.add_parser("dogfood", help="run the real-world dogfood")
    rw_dogfood.add_argument("--root", default=".artifacts/m064-m071/dogfood")
    rw_dogfood.add_argument("--real-runs-dir", dest="real_runs_dir",
                            default=dogfood_module.DEFAULT_REAL_RUNS)
    rw_dogfood.add_argument("--docs-dir", dest="docs_dir",
                            default=dogfood_module.DEFAULT_DOCS_DIR)
    rw_dogfood.add_argument("--json", action="store_true")

    acc = rw.add_parser("acceptance", help="run the acceptance matrix")
    acc.add_argument("--root", default=".artifacts/m064-m071/acceptance")
    acc.add_argument("--out", default=None)
    acc.add_argument("--json", action="store_true")

    portfolio = rw.add_parser(
        "portfolio", help="build the privacy-safe external evidence pack")
    portfolio.add_argument("--root", default=DEFAULT_DEMO_ROOT)
    portfolio.add_argument("--no-acceptance", dest="no_acceptance",
                           action="store_true")
    portfolio.add_argument("--json", action="store_true")

    demo = sub.add_parser("demo", help="reproducible privacy-safe demo")
    demo_sub = demo.add_subparsers(dest="demo_command", required=True)
    demo_portfolio = demo_sub.add_parser(
        "portfolio", help="run the full privacy-safe portfolio demo")
    demo_portfolio.add_argument("--root", default=None)
    demo_portfolio.add_argument("--no-acceptance", dest="no_acceptance",
                                action="store_true")
    demo_portfolio.add_argument("--json", action="store_true")


def is_realworld_command(command: str) -> bool:
    return command in ("realworld", "demo")


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "demo":
        if args.demo_command == "portfolio":
            return _cmd_demo_portfolio(args)
        return EXIT_USAGE
    handlers: dict[str, Any] = {
        "ingest": _cmd_ingest, "career": _cmd_career,
        "life-sciences": _cmd_life_sciences, "operations": _cmd_operations,
        "outcomes": _cmd_outcomes, "refresh": _cmd_refresh,
        "cockpit": _cmd_cockpit, "dogfood": _cmd_dogfood,
        "acceptance": _cmd_acceptance, "portfolio": _cmd_portfolio,
    }
    handler = handlers.get(args.realworld_command)
    if handler is None:
        return EXIT_USAGE
    try:
        return int(handler(args))
    except (intel_model.IntelligenceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED


__all__ = ["dispatch", "is_realworld_command", "register"]
