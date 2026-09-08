from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import suppress
from pathlib import Path

from . import web_server
from .audit import audit
from .common import InvalidDataError, ResourceBoundError, canonical_bytes
from .evaluator import evaluate
from .generator import generate
from .matcher import match
from .paths import PathSafetyError, validate_paths
from .publication import publish_bytes
from .scoring import default_config_bytes


def _workspace_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], name: str
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name)
    parser.add_argument("--workspace", required=True, help="explicit local workspace root")
    return parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="erw", description="Deterministic local entity-resolution workbench"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = _workspace_parser(subparsers, "generate")
    generate_parser.add_argument("--output", default="data")
    generate_parser.add_argument("--config", default="config/matching-v1.json")
    generate_parser.add_argument("--seed", type=int, default=20260902)

    match_parser = _workspace_parser(subparsers, "match")
    match_parser.add_argument("--observed-manifest", default="data/observed-manifest.json")
    match_parser.add_argument("--observed-root", default="data/observed")
    match_parser.add_argument("--database", default="state.sqlite")
    match_parser.add_argument("--predictions", default="predictions.json")
    match_parser.add_argument("--config", default="config/matching-v1.json")

    evaluate_parser = _workspace_parser(subparsers, "evaluate")
    evaluate_parser.add_argument("--observed-manifest", default="data/observed-manifest.json")
    evaluate_parser.add_argument("--truth-manifest", default="data/truth-manifest.json")
    evaluate_parser.add_argument("--truth-root", default="data/ground_truth")
    evaluate_parser.add_argument("--database", default="state.sqlite")
    evaluate_parser.add_argument("--predictions", default="predictions.json")
    evaluate_parser.add_argument("--report-json", default="report.json")
    evaluate_parser.add_argument("--report-html", default="report.html")

    audit_parser = _workspace_parser(subparsers, "audit")
    audit_parser.add_argument("--observed-manifest", default="data/observed-manifest.json")
    audit_parser.add_argument("--truth-manifest", default="data/truth-manifest.json")
    audit_parser.add_argument("--truth-root", default="data/ground_truth")
    audit_parser.add_argument("--database", default="state.sqlite")
    audit_parser.add_argument("--predictions", default="predictions.json")
    audit_parser.add_argument("--report-json", default="report.json")
    audit_parser.add_argument("--report-html", default="report.html")

    demo_parser = _workspace_parser(subparsers, "demo")
    demo_parser.add_argument("--seed", type=int, default=20260902)

    workbench_parser = _workspace_parser(subparsers, "workbench")
    workbench_parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    workbench_parser.add_argument("--port", type=_port, default=8765)
    workbench_parser.add_argument(
        "--ready-fd", type=_file_descriptor, default=None, help=argparse.SUPPRESS
    )
    return parser


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _file_descriptor(value: str) -> int:
    try:
        descriptor = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ready descriptor must be an integer") from exc
    if not 3 <= descriptor <= 1_024:
        raise argparse.ArgumentTypeError("ready descriptor is outside the supported range")
    return descriptor


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _bootstrap_config(root: Path, target: Path) -> None:
    validate_paths(root, outputs=(target,))
    publish_bytes(target, default_config_bytes())


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(canonical_bytes(payload).decode("utf-8"))


def _generate(root: Path, args: argparse.Namespace) -> dict[str, object]:
    config = _path(root, str(args.config))
    if not config.exists():
        _bootstrap_config(root, config)
    output = _path(root, str(args.output))
    result = generate(
        workspace_root=root,
        output=output,
        seed=int(args.seed),
        config_json=config,
    )
    return {
        "command": "generate",
        "ok": True,
        "observed_manifest": str(result.observed_manifest_json),
        "truth_manifest": str(result.truth_manifest_json),
    }


def _match(root: Path, args: argparse.Namespace) -> dict[str, object]:
    result = match(
        workspace_root=root,
        observed_manifest=_path(root, str(args.observed_manifest)),
        observed_root=_path(root, str(args.observed_root)),
        database=_path(root, str(args.database)),
        predictions_json=_path(root, str(args.predictions)),
        config_json=_path(root, str(args.config)),
    )
    return {
        "command": "match",
        "ok": True,
        "prediction_digest": result.prediction_digest,
        "predictions": str(_path(root, str(args.predictions))),
        "reused": result.reused,
        "run_key": result.run_key,
    }


def _evaluate(root: Path, args: argparse.Namespace) -> dict[str, object]:
    result = evaluate(
        workspace_root=root,
        observed_manifest=_path(root, str(args.observed_manifest)),
        truth_manifest=_path(root, str(args.truth_manifest)),
        predictions_json=_path(root, str(args.predictions)),
        truth_root=_path(root, str(args.truth_root)),
        database=_path(root, str(args.database)),
        report_json=_path(root, str(args.report_json)),
        report_html=_path(root, str(args.report_html)),
    )
    return {
        "command": "evaluate",
        "evaluation_key": result.evaluation_key,
        "metrics_by_split": result.metrics_by_split or {},
        "ok": True,
        "report_html": str(_path(root, str(args.report_html))),
        "report_json": str(_path(root, str(args.report_json))),
        "report_bundle_digest": result.report_bundle_digest,
    }


def _audit(root: Path, args: argparse.Namespace) -> dict[str, object]:
    result = audit(
        workspace_root=root,
        database=_path(root, str(args.database)),
        observed_manifest=_path(root, str(args.observed_manifest)),
        truth_manifest=_path(root, str(args.truth_manifest)),
        predictions_json=_path(root, str(args.predictions)),
        truth_root=_path(root, str(args.truth_root)),
        report_json=_path(root, str(args.report_json)),
        report_html=_path(root, str(args.report_html)),
    )
    return {"command": "audit", **result}


def _demo(root: Path, seed: int) -> dict[str, object]:
    config = root / "config" / "matching-v1.json"
    if not config.exists():
        _bootstrap_config(root, config)
    generated = generate(
        workspace_root=root,
        output=root / "data",
        seed=seed,
        config_json=config,
    )
    matched = match(
        workspace_root=root,
        observed_manifest=generated.observed_manifest_json,
        observed_root=root / "data" / "observed",
        database=root / "state.sqlite",
        predictions_json=root / "predictions.json",
        config_json=config,
    )
    evaluated = evaluate(
        workspace_root=root,
        observed_manifest=generated.observed_manifest_json,
        truth_manifest=generated.truth_manifest_json,
        predictions_json=root / "predictions.json",
        truth_root=root / "data" / "ground_truth",
        database=root / "state.sqlite",
        report_json=root / "report.json",
        report_html=root / "report.html",
    )
    checks = audit(
        workspace_root=root,
        database=root / "state.sqlite",
        observed_manifest=generated.observed_manifest_json,
        truth_manifest=generated.truth_manifest_json,
        predictions_json=root / "predictions.json",
        truth_root=root / "data" / "ground_truth",
        report_json=root / "report.json",
        report_html=root / "report.html",
    )
    return {
        "command": "demo",
        "database": str(root / "state.sqlite"),
        "evaluation_key": evaluated.evaluation_key,
        "metrics_by_split": evaluated.metrics_by_split or {},
        "observed_manifest": str(generated.observed_manifest_json),
        "ok": checks["ok"],
        "predictions": str(root / "predictions.json"),
        "report_html": str(root / "report.html"),
        "report_json": str(root / "report.json"),
        "run_key": matched.run_key,
        "truth_manifest": str(generated.truth_manifest_json),
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(str(args.workspace)).absolute()
    try:
        root.mkdir(parents=True, exist_ok=True)
        if args.command == "workbench":
            with suppress(KeyboardInterrupt):
                web_server.serve(
                    host=str(args.host),
                    port=int(args.port),
                    ready_fd=int(args.ready_fd) if args.ready_fd is not None else None,
                )
            return 0
        if args.command == "generate":
            payload = _generate(root, args)
        elif args.command == "match":
            payload = _match(root, args)
        elif args.command == "evaluate":
            payload = _evaluate(root, args)
        elif args.command == "audit":
            payload = _audit(root, args)
        else:
            payload = _demo(root, int(args.seed))
    except ResourceBoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except (InvalidDataError, PathSafetyError, sqlite3.DatabaseError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    _emit(payload)
    return 0
