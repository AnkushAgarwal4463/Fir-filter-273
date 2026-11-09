#!/usr/bin/env python3
"""Combine per-configuration FIR artifacts into one auditable CSV.

The output is an archive/status table, NOT the ML predictor matrix. Train from
per-run preplacement_features.csv and use physical_targets.csv as labels.
Missing configurations are retained as rows with blank measurements.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def parse_periods(raw: str) -> list[float]:
    values = [float(x.strip()) for x in raw.split(",") if x.strip()]
    if not values or any(x <= 0 for x in values):
        raise ValueError("--clock-periods must contain positive comma-separated values")
    return values


def run_id(taps: int, width: int, util: int, period: float, repair: str = "tr0") -> str:
    acc = width + 16  # COEFF_WIDTH=8 and the project accumulator rule is +8 guard bits.
    base = f"fir_t{taps}_dw{width}_cw8_aw{acc}_u{util}_clk{period:g}"
    return f"{base}_{repair}" if repair else base


def expected_configurations(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.run_mode == "single":
        configs = [{
            "taps": args.single_taps,
            "data_width": args.single_data_width,
            "utilization": args.single_utilization,
            "clock_period": args.single_clock_period,
        }]
    else:
        base = [
            (taps, width, util)
            for taps, widths in ((4, (8, 16, 24, 32, 40, 48)),
                                 (8, (8, 16, 24, 32, 40, 48)),
                                 (12, (8,)))
            for width in widths
            for util in (20, 30, 40, 50, 60, 70, 80)
        ]
        if len(base) != 91:
            raise RuntimeError(f"Internal sweep definition error: {len(base)} base configurations")
        configs = [
            {"taps": t, "data_width": w, "utilization": u, "clock_period": p}
            for t, w, u in base for p in args.clock_periods
        ]
    for cfg in configs:
        cfg["coeff_width"] = 8
        cfg["acc_width"] = cfg["data_width"] + 16
        cfg["place_density"] = cfg["utilization"] / 100.0
        cfg["run_id"] = run_id(cfg["taps"], cfg["data_width"], cfg["utilization"], cfg["clock_period"], "tr0")
    return configs


def read_csv_record(path: Path) -> dict[str, str] | None:
    try:
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        return rows[-1] if rows else None
    except (OSError, csv.Error, UnicodeError):
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def index_artifacts(root: Path):
    runs: dict[str, Path] = {}
    feature_files: dict[str, Path] = {}
    compact_files: dict[str, Path] = {}
    extraction_files: dict[str, Path] = {}
    status_files: dict[str, Path] = {}

    for path in root.rglob("configuration.json"):
        data = read_json(path) or {}
        rid = str(data.get("run_id") or path.parent.name)
        runs[rid] = path.parent
    for path in root.rglob("features.csv"):
        # The per-run extractor's combined record includes netlist cell_count.
        row = read_csv_record(path)
        if row is not None and ("cell_count" in row or "congestion_status" in row):
            feature_files[path.parent.name] = path
    for path in root.rglob("fir_features.csv"):
        compact_files[path.parent.name] = path
    for path in root.rglob("extraction_status.json"):
        extraction_files[path.parent.name] = path
    for path in root.rglob("matrix-status/*.json"):
        status_files[path.stem] = path
    # Some artifact download layouts flatten the matrix-status directory.
    for path in root.rglob("*.json"):
        if path.name in {"configuration.json", "extraction_status.json"}:
            continue
        if path.parent.name == "matrix-status":
            status_files[path.stem] = path
    return runs, feature_files, compact_files, extraction_files, status_files


def resolve_id(wanted: str, maps: tuple[dict, ...]) -> str | None:
    candidates = (wanted, wanted.removesuffix("_tr0"), wanted.removesuffix("_tr1"),
                  wanted.removesuffix("_tr0") + "_tr1")
    for candidate in candidates:
        if any(candidate in mapping for mapping in maps):
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("combined_fir_features.csv"))
    parser.add_argument("--run-mode", choices=("sweep", "single"), default="sweep")
    parser.add_argument("--single-taps", type=int, default=8)
    parser.add_argument("--single-data-width", type=int, default=8)
    parser.add_argument("--single-utilization", type=int, default=40)
    parser.add_argument("--single-clock-period", type=float, default=10.0)
    parser.add_argument("--clock-periods", default="5,7.5,10")
    args = parser.parse_args()
    try:
        args.clock_periods = parse_periods(args.clock_periods)
        configs = expected_configurations(args)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))

    if args.run_mode == "sweep" and len(configs) != 273:
        parser.error(f"Expected 273 configurations, generated {len(configs)}")

    maps = index_artifacts(args.artifacts)
    all_rows: list[dict[str, Any]] = []
    for cfg in configs:
        wanted = cfg["run_id"]
        actual = resolve_id(wanted, maps)
        run_dir = maps[0].get(actual) if actual else None
        feature_path = maps[1].get(actual) if actual else None
        compact_path = maps[2].get(actual) if actual else None
        extraction_path = maps[3].get(actual) if actual else None
        status_path = maps[4].get(actual) if actual else None

        feature = read_csv_record(feature_path) if feature_path else {}
        compact = read_csv_record(compact_path) if compact_path else {}
        extraction = read_json(extraction_path) if extraction_path else {}
        status = read_json(status_path) if status_path else {}
        feature = feature or {}
        compact = compact or {}
        extraction = extraction or {}
        status = status or {}

        extraction_ok = bool(extraction.get("extraction_success"))
        job_outcome = str(status.get("job_outcome") or "unknown").lower()
        flow_status = str(compact.get("flow_status") or "unknown")
        last_stage = str(status.get("last_completed_stage") or compact.get("last_completed_stage") or "unknown")
        final_def = str(feature.get("final_def_nonempty", "")).lower() in {"true", "1", "yes"}
        final_odb = str(feature.get("final_odb_available", "")).lower() in {"true", "1", "yes"}
        final_pair = str(feature.get("artifacts_present", "")).lower() in {"true", "1", "yes"}

        if extraction_ok and job_outcome == "success" and flow_status == "completed" and final_def and final_odb and final_pair:
            config_status = "artifacts_received_job_success"
        elif extraction_ok and job_outcome == "success":
            config_status = "artifacts_received_status_unconfirmed"
        elif extraction_ok:
            config_status = "artifacts_received_job_failed"
        elif run_dir or feature_path or compact_path or extraction_path:
            config_status = "artifacts_incomplete"
        elif status_path:
            config_status = "status_only_no_run_artifacts"
        else:
            config_status = "missing_artifact"

        row: dict[str, Any] = {
            **cfg,
            "run_id_found": actual or "",
            "configuration_status": config_status,
            "job_outcome": job_outcome,
            "physical_flow_status": flow_status,
            "last_completed_stage": last_stage,
            "failure_reason": status.get("failure_reason", ""),
            "extraction_success": extraction_ok,
            "run_directory_found": bool(run_dir),
            "features_csv_found": bool(feature_path),
            "fir_features_csv_found": bool(compact_path),
            "extraction_status_json_found": bool(extraction_path),
            "matrix_status_json_found": bool(status_path),
        }
        # Keep all extracted fields for audit/analysis. Pre-placement-only model
        # predictors remain in preplacement_features.csv, never this archive CSV.
        row.update(feature)
        for key, value in compact.items():
            row.setdefault(key, value)
        all_rows.append(row)

    fields: list[str] = []
    for row in all_rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    counts: dict[str, int] = {}
    for row in all_rows:
        key = str(row["configuration_status"])
        counts[key] = counts.get(key, 0) + 1
    print(f"Expected configurations: {len(configs)}")
    print(f"Rows written: {len(all_rows)}")
    print(f"Status counts: {json.dumps(counts, sort_keys=True)}")
    print(f"Combined CSV: {args.output}")
    print("ML reminder: use preplacement_features.csv as predictors and physical_targets.csv as labels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
