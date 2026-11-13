
#!/usr/bin/env python3
"""Extract FIR synthesis features and OpenROAD physical targets.

Dataset outputs:
    preplacement_features.csv/.json  - synthesized-netlist predictors only
    netlist_features.csv/.json       - aliases for the netlist predictors
    run_conditions.csv/.json         - pre-placement run conditions
    physical_targets.csv/.json       - placement/routing targets
    features.csv/.json               - combined inspection dataset
    fir_features.csv                 - FIR configuration and flow status
    extraction_status.json           - extraction diagnostics
    run_report.md                    - human-readable report
    fir_feature_report.xlsx          - optional Excel workbook

Unavailable measurements remain null. No physical result is fabricated.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys

from collections import Counter
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"

SEQ_RE = re.compile(
    r"(?:sky130_fd_sc_hd__(?:df|sd|ed|sdf)|"
    r"\b(?:dfrtp|dfrbp|dfrtn|dfr|dff|flipflop|latch)\b)",
    re.I,
)

BUF_RE = re.compile(r"(?:buf|inv|clkbuf)", re.I)

COMB_RE = re.compile(
    r"(?:and|or|xor|xnor|mux|maj|fa|ha|aoi|oai|nand|nor|add|sub|mul|alu|logic|cmp)",
    re.I,
)


# ---------------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------------

def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def finite_float(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, allow_nan=False)
        handle.write("\n")


def write_csv(path: Path, data: dict[str, Any]) -> None:
    """Write one record, preserving nested diagnostics as JSON text."""
    flat = {
        key: (
            json.dumps(value, sort_keys=True, ensure_ascii=False)
            if isinstance(value, (dict, list, tuple))
            else value
        )
        for key, value in data.items()
    }

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(flat))
        writer.writeheader()
        writer.writerow(flat)


def write_rows_csv(
    path: Path,
    fields: list[str],
    row: dict[str, Any],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({key: row.get(key) for key in fields})


def last_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        matches = list(re.finditer(pattern, text, re.I | re.M))

        for match in reversed(matches):
            value = finite_float(match.group(1))
            if value is not None:
                return value

    return None


def first_manifest_value(
    manifest: dict[str, Any],
    keys: tuple[str, ...],
    fallback: Any = None,
) -> Any:
    """Return the first present, non-null manifest value."""
    for key in keys:
        if key in manifest and manifest[key] is not None:
            return manifest[key]
    return fallback


def manifest_number(
    manifest: dict[str, Any],
    keys: tuple[str, ...],
    fallback: Any = None,
) -> float | None:
    return finite_float(first_manifest_value(manifest, keys, fallback))


def safe_relative_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except (OSError, ValueError):
        return str(path)


# ---------------------------------------------------------------------------
# Log collection
# ---------------------------------------------------------------------------

def collect_logs(run_dir: Path) -> dict[str, str]:
    """Collect .log/.txt files recursively, including nested OpenROAD logs."""
    if not run_dir.is_dir():
        return {}

    paths = sorted(
        path
        for path in run_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".log", ".txt"}
    )

    priority = {
        "5_2_route.log": 0,
        "detailed_route.log": 1,
        "openroad_log.txt": 2,
        "flow_diagnostics.log": 3,
    }

    paths.sort(
        key=lambda path: (
            priority.get(path.name.lower(), 4),
            str(path),
        )
    )

    result: dict[str, str] = {}

    for path in paths:
        content = read_text(path)
        if content.strip():
            result[safe_relative_path(path, run_dir)] = content

    return result


def choose_routing_log(
    logs: dict[str, str],
) -> tuple[str | None, str]:
    priority = (
        "5_2_route.log",
        "detailed_route.log",
        "openroad_log.txt",
        "flow_diagnostics.log",
    )

    for wanted in priority:
        for name, content in logs.items():
            if (
                Path(name).name.lower() == wanted
                and re.search(
                    r"detailed routing|detail routing|total wire length|"
                    r"number of vias|violations",
                    content,
                    re.I,
                )
            ):
                return name, content

    if logs:
        return "combined_logs", "\n".join(logs.values())

    return None, ""


# ---------------------------------------------------------------------------
# Technology and netlist parsing
# ---------------------------------------------------------------------------

def classify_cell(cell_type: str) -> str:
    """Heuristic cell classification; validate against Liberty for research."""
    name = str(cell_type).lower().lstrip("\\")

    if SEQ_RE.search(name):
        return "sequential"

    if BUF_RE.search(name):
        return "buffer_inverter"

    if COMB_RE.search(name):
        return "combinational"

    return "other"


def load_netlist(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Netlist JSON not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON netlist: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("modules"), dict):
        raise ValueError(
            "Expected a Yosys JSON file containing 'modules'."
        )

    return data


def select_top(
    modules: dict[str, Any],
    requested_top: str,
) -> str:
    if requested_top in modules:
        return requested_top

    marked = []

    for name, module in modules.items():
        value = str(
            module.get("attributes", {}).get("top", "")
        ).strip("'")

        if value.lstrip("0") == "1":
            marked.append(name)

    if len(marked) == 1:
        return marked[0]

    if len(modules) == 1:
        return next(iter(modules))

    raise ValueError(
        f"Top '{requested_top}' not found. Available: {list(modules)}"
    )


def configured_path(name: str) -> Path | None:
    """Read a path from the environment or config/sky130_example.env."""
    value = os.environ.get(name)

    if value:
        return Path(value.strip().strip("\"'"))

    config_path = Path("config/sky130_example.env")

    if not config_path.is_file():
        return None

    pattern = re.compile(
        rf"^\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*?)\s*$"
    )

    for line in read_text(config_path).splitlines():
        match = pattern.match(line)

        if not match:
            continue

        value = (
            match.group(1)
            .split("#", 1)[0]
            .strip()
            .strip("\"'")
        )

        value = re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]+)\}",
            lambda item: os.environ.get(item.group(1), item.group(2)),
            value,
        )

        value = os.path.expandvars(value)

        if value:
            return Path(value)

    return None


def balanced_block(text: str, open_brace: int) -> str:
    """Return a brace-delimited block body, respecting nested braces."""
    depth = 0

    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1

            if depth == 0:
                return text[open_brace + 1:index]

    return ""


def load_liberty_pin_directions(
    path: Path | None,
) -> dict[str, dict[str, str]]:
    """Read cell pin directions from Liberty as a fallback for Yosys JSON."""
    if path is None or not path.is_file():
        return {}

    text = read_text(path)
    result: dict[str, dict[str, str]] = {}

    cell_pattern = re.compile(
        r'\bcell\s*\(\s*"?([^\)"\s]+)"?\s*\)\s*\{',
        re.I,
    )

    for match in cell_pattern.finditer(text):
        cell_name = match.group(1)
        body = balanced_block(text, match.end() - 1)

        if not body:
            continue

        pins: dict[str, str] = {}

        pin_pattern = re.compile(
            r'\bpin\s*\(\s*"?([^\)"\s]+)"?\s*\)\s*\{',
            re.I,
        )

        for pin_match in pin_pattern.finditer(body):
            pin_name = pin_match.group(1)
            pin_body = balanced_block(body, pin_match.end() - 1)

            direction = re.search(
                r'\bdirection\s*:\s*"?'
                r'(input|output|inout)"?\s*;',
                pin_body,
                re.I,
            )

            if direction:
                pins[pin_name] = direction.group(1).lower()

        if pins:
            result[cell_name] = pins

    return result


def load_lef_sizes(
    paths: list[Path | None] | Path | str | None,
) -> tuple[
    dict[str, tuple[float, float]],
    dict[str, tuple[float, float]],
]:
    """Read standard-cell macro and site dimensions from LEF files."""
    if paths is None:
        paths = []
    elif isinstance(paths, (str, Path)):
        paths = [Path(paths)]

    macros: dict[str, tuple[float, float]] = {}
    sites: dict[str, tuple[float, float]] = {}

    size_pattern = re.compile(
        rf"\bSIZE\s+({NUM})\s+BY\s+({NUM})\s*;",
        re.I,
    )

    for path in paths:
        if path is None:
            continue

        path = Path(path)

        if not path.is_file():
            continue

        text = read_text(path)

        for kind, target in (
            ("MACRO", macros),
            ("SITE", sites),
        ):
            pattern = re.compile(
                rf"(?mi)^\s*{kind}\s+(\S+)\s*$"
            )

            for match in pattern.finditer(text):
                name = match.group(1)

                end = re.search(
                    rf"(?mi)^\s*END\s+{re.escape(name)}\s*$",
                    text[match.end():],
                )

                if not end:
                    continue

                body = text[
                    match.end():match.end() + end.start()
                ]

                size = size_pattern.search(body)

                if size:
                    target[name] = (
                        float(size.group(1)),
                        float(size.group(2)),
                    )

    return macros, sites


# ---------------------------------------------------------------------------
# Achieved placement-density measurement
# ---------------------------------------------------------------------------

def measure_density_from_def(
    def_text: str,
    lef_path: Path | None,
) -> dict[str, Any]:
    """Compute placed standard-cell area / placement-row area from DEF + LEF.

    This is an achieved-density estimate, not the requested placement target.
    """
    tech_lef_path = configured_path("TECH_LEF")
    cell_lef_path = lef_path or configured_path("CELL_LEF")

    macros, sites = load_lef_sizes([
        tech_lef_path,
        cell_lef_path,
    ])

    unavailable = {
        "effective_place_density": None,
        "placed_cell_area_from_def_um2": None,
        "placement_row_area_um2": None,
        "effective_place_density_method": (
            "unavailable: DEF/LEF geometry incomplete"
        ),
    }

    if not def_text or not macros or not sites:
        return unavailable

    component_section = re.search(
        r"\bCOMPONENTS\s+\d+\s*;(.*?)\bEND\s+COMPONENTS\b",
        def_text,
        re.I | re.S,
    )

    placed_area = 0.0
    placed_cells = 0

    if component_section:
        entries = re.findall(
            r"(?ms)^[ \t]*-[ \t]+.*?;",
            component_section.group(1),
        )

        for entry in entries:
            head = re.match(
                r"(?s)\s*-\s+(\S+)\s+(\S+)(.*)",
                entry,
            )

            if not head:
                continue

            cell_type = head.group(2)
            rest = head.group(3)

            if not re.search(
                r"\+\s*(?:PLACED|FIXED|COVER)\b",
                rest,
                re.I,
            ):
                continue

            size = macros.get(cell_type)

            if size:
                placed_area += size[0] * size[1]
                placed_cells += 1

    row_area = 0.0
    row_count = 0

    row_pattern = re.compile(
        rf"(?mi)^\s*ROW\s+\S+\s+(\S+)\s+{NUM}\s+{NUM}\s+"
        rf"\S+\s+DO\s+(\d+)\s+BY\s+(\d+)\s+STEP\s+{NUM}\s+{NUM}\s*;"
    )

    for row in row_pattern.finditer(def_text):
        site_name = row.group(1)
        dimensions = sites.get(site_name)

        if not dimensions:
            continue

        nx = int(row.group(2))
        ny = int(row.group(3))

        row_area += nx * ny * dimensions[0] * dimensions[1]
        row_count += 1

    density = (
        placed_area / row_area
        if placed_area > 0 and row_area > 0
        else None
    )

    return {
        "effective_place_density": density,
        "placed_cell_area_from_def_um2": (
            placed_area if placed_area > 0 else None
        ),
        "placement_row_area_um2": (
            row_area if row_area > 0 else None
        ),
        "effective_place_density_method": (
            (
                "placed LEF macro area / DEF placement-row area; "
                f"{placed_cells} placed cells, {row_count} rows"
            )
            if density is not None
            else (
                "unavailable: no placed cells or placement rows "
                "could be measured"
            )
        ),
    }


# ---------------------------------------------------------------------------
# Synthesized-netlist feature extraction
# ---------------------------------------------------------------------------

def extract_netlist_features(
    path: Path,
    requested_top: str,
    design_id: str,
) -> dict[str, Any]:
    design = load_netlist(path)
    top = select_top(design["modules"], requested_top)
    module = design["modules"][top]

    cells = module.get("cells", {})
    ports = module.get("ports", {})
    netnames = module.get("netnames", {})

    if not isinstance(cells, dict):
        raise ValueError("Invalid cells object in Yosys JSON.")

    cell_types = Counter(
        str(cell.get("type", "unknown"))
        for cell in cells.values()
    )

    classes = Counter()

    for cell_type, count in cell_types.items():
        classes[classify_cell(cell_type)] += count

    liberty_path = configured_path("LIBERTY")
    liberty_directions = load_liberty_pin_directions(liberty_path)

    liberty_by_name = {
        name.strip().strip('"').lstrip("\\").lower(): pins
        for name, pins in liberty_directions.items()
    }

    all_bits: set[int] = set()
    sink_counts: Counter[int] = Counter()

    port_count_total = 0
    port_count_known = 0

    cells_with_any_directions = 0
    cells_with_complete_directions = 0
    connected_cells = 0

    for obj in list(netnames.values()) + list(ports.values()):
        for bit in obj.get("bits", []):
            if isinstance(bit, int) and not isinstance(bit, bool):
                all_bits.add(bit)

    for cell in cells.values():
        connections = cell.get("connections", {})
        directions = cell.get("port_directions", {})

        if not isinstance(connections, dict):
            connections = {}

        if not isinstance(directions, dict):
            directions = {}

        if not connections:
            # Cells with no connected pins do not contribute to the
            # fanout-direction completeness denominator.
            continue

        connected_cells += 1

        cell_type = (
            str(cell.get("type", ""))
            .strip()
            .strip('"')
            .lstrip("\\")
        )

        liberty_cell = liberty_by_name.get(
            cell_type.lower(),
            {},
        )

        resolved = {
            str(pin_name).strip().strip('"').lower(): str(direction).lower()
            for pin_name, direction in liberty_cell.items()
        }

        # Yosys metadata takes precedence over Liberty fallback.
        resolved.update({
            str(pin_name).strip().strip('"').lower(): str(direction).lower()
            for pin_name, direction in directions.items()
        })

        if resolved:
            cells_with_any_directions += 1

        complete = True

        for port_name, bits in connections.items():
            direction = resolved.get(
                str(port_name).strip().strip('"').lower(),
                "",
            )

            port_count_total += 1

            if direction in {"input", "output", "inout"}:
                port_count_known += 1
            else:
                complete = False

            for bit in bits:
                if isinstance(bit, int) and not isinstance(bit, bool):
                    all_bits.add(bit)

                    if direction == "input":
                        sink_counts[bit] += 1

        if complete:
            cells_with_complete_directions += 1

    coverage = (
        port_count_known / port_count_total
        if port_count_total
        else 0.0
    )

    complete_all = (
        connected_cells > 0
        and cells_with_complete_directions == connected_cells
    )

    fanouts = (
        [
            sink_counts[bit]
            for bit in all_bits
            if sink_counts[bit] > 0
        ]
        if complete_all
        else []
    )

    net_bit_total = sum(
        sum(
            isinstance(bit, int) and not isinstance(bit, bool)
            for bit in net.get("bits", [])
        )
        for net in netnames.values()
    )

    if complete_all:
        fanout_method = (
            "Yosys port_directions with Liberty fallback; "
            "input-pin sink count per connected net bit"
        )
        fanout_warning = ""
    else:
        fanout_method = (
            "unavailable: some connected cell ports have "
            "no resolved direction"
        )
        fanout_warning = (
            f"Resolved directions for {port_count_known}/"
            f"{port_count_total} cell ports ({coverage:.1%}); "
            f"complete directions for "
            f"{cells_with_complete_directions}/{connected_cells} "
            f"connected cells."
        )

    return {
        "design_id": design_id,
        "top_module": top,
        "netlist_json": str(path),
        "cell_count": len(cells),
        "netname_count": len(netnames),
        "port_count": len(ports),
        "port_bit_count": sum(
            len(port.get("bits", []))
            for port in ports.values()
        ),
        "sequential_cell_count": classes["sequential"],
        "combinational_cell_count": classes["combinational"],
        "buffer_inverter_count": classes["buffer_inverter"],
        "other_cell_count": classes["other"],
        "unique_cell_type_count": len(cell_types),
        "avg_bit_fanout": mean(fanouts) if fanouts else None,
        "max_bit_fanout": max(fanouts) if fanouts else None,
        "std_bit_fanout": (
            pstdev(fanouts)
            if len(fanouts) > 1
            else 0.0 if fanouts else None
        ),
        "fanout_direction_metadata_found": (
            cells_with_any_directions > 0
        ),
        "fanout_direction_coverage_fraction": coverage,
        "fanout_direction_coverage_percent": coverage * 100.0,
        "fanout_metadata_complete_for_all_cells": complete_all,
        "fanout_cells_with_any_directions": cells_with_any_directions,
        "fanout_cells_with_complete_directions": (
            cells_with_complete_directions
        ),
        "fanout_total_cells": connected_cells,
        "fanout_method": fanout_method,
        "fanout_quality_warning": fanout_warning,
        "total_net_bits": net_bit_total,
        "unique_cell_types": dict(cell_types),
        "feature_stage": "post-synthesis / pre-placement",
        "cell_classification_method": (
            "heuristic cell-name matching; validate against Liberty"
        ),
    }


# ---------------------------------------------------------------------------
# Timing report extraction
# ---------------------------------------------------------------------------

def parse_timing_metric(
    text: str,
    metric: str,
) -> float | None:
    """Extract explicitly identified setup or hold timing metrics.

    Generic worst-slack values are deliberately not accepted because
    they may refer to setup or hold depending on the report.
    """
    if not text:
        return None

    number = NUM

    patterns = {
        "wns_setup_ns": [
            rf"\bworst\s+slack\s+max\s*[:=]?\s*({number})",
            rf"\bwns\s+max\s*[:=]?\s*({number})",
            rf"\bsetup\s+(?:wns|worst\s+slack)\s*[:=]\s*({number})",
            rf"\b(?:setup\s+)?worst\s+slack\s*\(\s*max\s*\)\s*[:=]\s*({number})",
        ],
        "tns_setup_ns": [
            rf"\btotal\s+negative\s+slack\s+max\s*[:=]?\s*({number})",
            rf"\btns\s+max\s*[:=]?\s*({number})",
            rf"\bsetup\s+tns\s*[:=]\s*({number})",
            rf"\bsetup\s+total\s+negative\s+slack\s*[:=]\s*({number})",
        ],
        "wns_hold_ns": [
            rf"\bworst\s+slack\s+min\s*[:=]?\s*({number})",
            rf"\bwns\s+min\s*[:=]?\s*({number})",
            rf"\bhold\s+(?:wns|worst\s+slack)\s*[:=]\s*({number})",
            rf"\b(?:hold\s+)?worst\s+slack\s*\(\s*min\s*\)\s*[:=]\s*({number})",
        ],
    }

    if metric not in patterns:
        raise ValueError(f"Unsupported timing metric: {metric}")

    for pattern in patterns[metric]:
        matches = list(re.finditer(pattern, text, re.I | re.M))

        for match in reversed(matches):
            value = finite_float(match.group(1))

            if value is not None:
                return value

    return None


def find_report(
    run_dir: Path,
    candidates: list[str],
    metric: str | None = None,
) -> tuple[Path | None, str]:
    """Find a nonempty report, preferring one with a parseable metric."""
    seen: set[Path] = set()
    fallback: tuple[Path | None, str] = (None, "")

    for candidate in candidates:
        paths = [run_dir / candidate]
        paths.extend(sorted(run_dir.rglob(candidate)))

        for path in paths:
            if path in seen:
                continue

            seen.add(path)

            if not path.is_file():
                continue

            content = read_text(path)

            if not content.strip():
                continue

            if fallback[0] is None:
                fallback = (path, content)

            if metric is None:
                return path, content

            if parse_timing_metric(content, metric) is not None:
                return path, content

    return fallback


# ---------------------------------------------------------------------------
# Routing and congestion parsing
# ---------------------------------------------------------------------------

def parse_def_routing(text: str) -> dict[str, Any]:
    section = re.search(
        r"\bNETS\s+\d+\s*;(.*?)\bEND\s+NETS\b",
        text,
        re.I | re.S,
    )

    if not section:
        return {
            "def_nets_section_found": False,
            "routed_net_count": None,
            "route_segment_count": None,
        }

    entries = re.findall(
        r"(?ms)^[ \t]*-[ \t]+.*?;",
        section.group(1),
    )

    routed = 0
    clauses = 0

    for entry in entries:
        found = re.findall(
            r"\b(?:ROUTED|FIXED|COVER|NEW)\s+"
            r"[A-Za-z0-9_.$/\[\]-]+",
            entry,
            re.I,
        )

        if found:
            routed += 1

        clauses += len(
            re.findall(
                r"\b(?:ROUTED|NEW)\s+[A-Za-z0-9_.$/\[\]-]+",
                entry,
                re.I,
            )
        )

    return {
        "def_nets_section_found": True,
        "routed_net_count": routed,
        "route_segment_count": clauses,
    }


def parse_route_log(text: str) -> dict[str, Any]:
    wire = last_number(
        text,
        [
            rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})\s*(?:um|µm)\b",
            rf"\bTotal\s+wire\s+length\s*[:=]\s*({NUM})",
        ],
    )

    vias = last_number(
        text,
        [
            rf"\bTotal\s+number\s+of\s+vias\s*[:=]\s*({NUM})",
            rf"\bNumber\s+of\s+vias\s*[:=]\s*({NUM})",
        ],
    )

    violations = last_number(
        text,
        [
            rf"\bNumber\s+of\s+violations\s*[:=]\s*({NUM})",
            rf"\bTotal\s+violations\s*[:=]\s*({NUM})",
        ],
    )

    return {
        "wirelength_um": wire,
        "via_count": int(vias) if vias is not None else None,
        "final_detailed_route_violations": (
            int(violations) if violations is not None else None
        ),
    }


def empty_grt_metrics() -> dict[str, float | None]:
    """Return the stable set of global-routing congestion target fields."""
    metrics: dict[str, float | None] = {
        "grt_total_resource": None,
        "grt_total_demand": None,
        "grt_usage_percent": None,
        "grt_max_horizontal_congestion": None,
        "grt_max_vertical_congestion": None,
        "grt_total_congestion": None,
    }

    for layer in ("li1", "met1", "met2", "met3", "met4", "met5"):
        for suffix in (
            "resource",
            "demand",
            "usage_percent",
            "max_horizontal_congestion",
            "max_vertical_congestion",
            "total_congestion",
        ):
            metrics[f"grt_{layer}_{suffix}"] = None

    return metrics


def parse_congestion_metrics(
    text: str,
) -> dict[str, float | None]:
    """Parse OpenROAD GRT resource/demand/usage/congestion table."""
    result = empty_grt_metrics()

    if not text or not text.strip():
        return result

    headers = list(
        re.finditer(
            r"(?im)^.*Final congestion report:\s*$",
            text,
        )
    )

    table_text = (
        text[headers[-1].end():]
        if headers
        else text
    )

    row_pattern = re.compile(
        rf"(?im)^\s*(li1|met[1-5]|Total)\s+"
        rf"({NUM})\s+({NUM})\s+({NUM})%\s+"
        rf"({NUM})\s*/\s*({NUM})\s*/\s*({NUM})\s*$"
    )

    found_total = False

    for match in row_pattern.finditer(table_text):
        layer = match.group(1).lower()
        values = [
            finite_float(match.group(index))
            for index in range(2, 8)
        ]

        if any(value is None for value in values):
            continue

        resource, demand, usage, max_h, max_v, total = values

        if layer == "total":
            result.update({
                "grt_total_resource": resource,
                "grt_total_demand": demand,
                "grt_usage_percent": usage,
                "grt_max_horizontal_congestion": max_h,
                "grt_max_vertical_congestion": max_v,
                "grt_total_congestion": total,
            })

            found_total = True

        else:
            result.update({
                f"grt_{layer}_resource": resource,
                f"grt_{layer}_demand": demand,
                f"grt_{layer}_usage_percent": usage,
                f"grt_{layer}_max_horizontal_congestion": max_h,
                f"grt_{layer}_max_vertical_congestion": max_v,
                f"grt_{layer}_total_congestion": total,
            })

    return result if found_total else empty_grt_metrics()


def parse_congestion(text: str) -> float | None:
    """Compatibility accessor: returns GRT total congestion, not overflow."""
    return parse_congestion_metrics(text)["grt_total_congestion"]


def parse_congestion_status(
    text: str,
    key: str = "GLOBAL_ROUTE_HAS_CONGESTION",
) -> str | None:
    """Read only an explicitly written global-route Boolean status."""
    if not text:
        return None

    match = re.search(
        rf"(?im)^\s*{re.escape(key)}:\s*(true|false|0|1)\s*$",
        text,
    )

    if not match:
        return None

    return (
        "yes"
        if match.group(1).lower() in {"true", "1"}
        else "no"
    )


def find_global_route_source(
    run_dir: Path,
    logs: dict[str, str],
) -> tuple[str | None, str, dict[str, float | None]]:
    """Find a real GRT table in a report or OpenROAD log."""
    report_names = (
        "congestion.rpt",
        "global_route_congestion.rpt",
        "grt_congestion.rpt",
        "routing_congestion.rpt",
    )

    seen: set[Path] = set()

    for wanted in report_names:
        paths = [
            run_dir / wanted,
            *sorted(run_dir.rglob(wanted)),
        ]

        for path in paths:
            if path in seen or not path.is_file():
                continue

            seen.add(path)
            content = read_text(path)
            metrics = parse_congestion_metrics(content)

            if metrics["grt_total_resource"] is not None:
                return str(path), content, metrics

    for name, content in logs.items():
        base = Path(name).name.lower()

        if base != "openroad_log.txt" and base not in {
            "5_1_grt.log",
            "global_route.log",
            "global_routing.log",
            "global_route_congestion.log",
            "grt.log",
        }:
            continue

        if not re.search(
            r"(?im)^.*Final congestion report:",
            content,
        ):
            continue

        metrics = parse_congestion_metrics(content)

        if metrics["grt_total_resource"] is not None:
            return (
                f"{name} (GRT-0096 Final congestion report)",
                content,
                metrics,
            )

    return None, "", empty_grt_metrics()


def read_grt_summary(
    run_dir: Path,
    filename: str,
) -> tuple[str | None, dict[str, float | None]]:
    """Read a report_congestion summary written with OpenROAD tee."""
    path = run_dir / filename

    if not path.is_file():
        return None, empty_grt_metrics()

    metrics = parse_congestion_metrics(read_text(path))

    if metrics["grt_total_resource"] is None:
        return str(path), empty_grt_metrics()

    return str(path), metrics


# ---------------------------------------------------------------------------
# Physical log parsing
# ---------------------------------------------------------------------------

def parse_physical_log_values(text: str) -> dict[str, Any]:
    density = last_number(
        text,
        [
            rf"\buniform density\s+to\s+({NUM})",
            rf"\b(?:auto(?:matically)?\s+)?adjust(?:s|ed|ing)?\s+"
            rf"uniform density\s+to\s+({NUM})",
            rf"\beffective\s+(?:placement\s+)?density\s*[:=]\s*({NUM})",
            rf"\bachieved\s+(?:placement\s+)?density\s*[:=]\s*({NUM})",
        ],
    )

    area = last_number(
        text,
        [
            rf"\bmovable instances area\s*[:=]?\s*({NUM})",
            rf"\bdesign area\s*[:=]?\s*({NUM})",
        ],
    )

    return {
        "effective_place_density": density,
        "design_area_um2": area,
    }


# ---------------------------------------------------------------------------
# Physical-design target extraction
# ---------------------------------------------------------------------------

def physical_features(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_dir():
        raise FileNotFoundError(
            f"Run directory not found: {run_dir}"
        )

    def_path = run_dir / "final.def"
    pre_drt_def_path = run_dir / "pre_drt.def"
    odb_path = run_dir / "final.odb"

    def_text = read_text(def_path)
    pre_drt_def_text = read_text(pre_drt_def_path)

    placement_def_text = (
        def_text
        if def_text.strip()
        else pre_drt_def_text
    )

    setup_path, setup_text = find_report(
        run_dir,
        [
            "wns_setup.rpt",
            "timing_setup.rpt",
            "wns_setup_global_route.rpt",
            "timing_setup_global_route.rpt",
            "setup_timing.rpt",
            "setup.rpt",
        ],
        metric="wns_setup_ns",
    )

    tns_path, tns_text = find_report(
        run_dir,
        [
            "tns_setup.rpt",
            "tns_setup_global_route.rpt",
            "timing_setup.rpt",
            "timing_setup_global_route.rpt",
            "setup_timing.rpt",
            "setup.rpt",
        ],
        metric="tns_setup_ns",
    )

    hold_path, hold_text = find_report(
        run_dir,
        [
            "wns_hold.rpt",
            "timing_hold.rpt",
            "wns_hold_global_route.rpt",
            "hold_timing.rpt",
            "hold.rpt",
        ],
        metric="wns_hold_ns",
    )

    area_path, area_text = find_report(
        run_dir,
        ["design_area.rpt", "area.rpt"],
    )

    logs = collect_logs(run_dir)
    route_name, route_text = choose_routing_log(logs)
    all_logs = "\n".join(logs.values())

    congestion_source, congestion_metrics = read_grt_summary(
        run_dir,
        "final_congestion_summary.rpt",
    )

    if congestion_metrics["grt_total_resource"] is None:
        (
            congestion_source,
            congestion_text,
            congestion_metrics,
        ) = find_global_route_source(run_dir, logs)
    else:
        congestion_text = read_text(
            run_dir / "final_congestion_summary.rpt"
        )

    (
        initial_congestion_source,
        initial_congestion_raw,
    ) = read_grt_summary(
        run_dir,
        "initial_congestion_summary.rpt",
    )

    initial_congestion_metrics = {
        f"initial_{key}": value
        for key, value in initial_congestion_raw.items()
    }

    status_path = run_dir / "congestion_status.rpt"

    if not status_path.is_file():
        status_path = run_dir / "congestion_overflow.rpt"

    status_text = read_text(status_path)

    congestion_status = parse_congestion_status(status_text)

    if congestion_status is None:
        congestion_status = parse_congestion_status(
            congestion_text
        )

    initial_congestion_status = parse_congestion_status(
        status_text,
        "INITIAL_GLOBAL_ROUTE_HAS_CONGESTION",
    )

    wns_setup = parse_timing_metric(
        setup_text,
        "wns_setup_ns",
    )

    tns_setup = parse_timing_metric(
        tns_text,
        "tns_setup_ns",
    )

    wns_hold = parse_timing_metric(
        hold_text,
        "wns_hold_ns",
    )

    if wns_setup is None:
        wns_setup = parse_timing_metric(
            all_logs,
            "wns_setup_ns",
        )

        if wns_setup is not None:
            setup_path = None

    if tns_setup is None:
        tns_setup = parse_timing_metric(
            all_logs,
            "tns_setup_ns",
        )

        if tns_setup is not None:
            tns_path = None

    if wns_hold is None:
        wns_hold = parse_timing_metric(
            all_logs,
            "wns_hold_ns",
        )

        if wns_hold is not None:
            hold_path = None

    setup_src = (
        str(setup_path)
        if setup_path is not None and wns_setup is not None
        else "logs" if wns_setup is not None
        else None
    )

    tns_src = (
        str(tns_path)
        if tns_path is not None and tns_setup is not None
        else "logs" if tns_setup is not None
        else None
    )

    hold_src = (
        str(hold_path)
        if hold_path is not None and wns_hold is not None
        else "logs" if wns_hold is not None
        else None
    )

    route = parse_route_log(route_text)

    physical = parse_physical_log_values(
        "\n".join([area_text, all_logs])
    )

    lef_path = configured_path("CELL_LEF")

    density_measurement = measure_density_from_def(
        placement_def_text,
        lef_path,
    )

    # Prefer geometry-based achieved density. Fall back only to an
    # explicit achieved/effective density message from OpenROAD.
    if (
        density_measurement.get("effective_place_density") is None
        and physical.get("effective_place_density") is not None
    ):
        density_measurement = {
            **density_measurement,
            "effective_place_density": physical[
                "effective_place_density"
            ],
            "effective_place_density_method": (
                "explicit achieved/effective density reported "
                "in OpenROAD log"
            ),
        }

    def_counts = (
        parse_def_routing(def_text)
        if def_text
        else {
            "def_nets_section_found": False,
            "routed_net_count": None,
            "route_segment_count": None,
        }
    )

    route_success = bool(
        re.search(
            r"SUCCESS:\s*Detailed routing|"
            r"Complete detail routing|"
            r"Detailed routing completed",
            all_logs,
            re.I,
        )
    )

    def_ok = (
        def_path.is_file()
        and def_path.stat().st_size > 0
    )

    odb_ok = (
        odb_path.is_file()
        and odb_path.stat().st_size > 0
    )

    measurements = {
        "wns_setup_ns": (
            "measured" if wns_setup is not None else "unavailable"
        ),
        "tns_setup_ns": (
            "measured" if tns_setup is not None else "unavailable"
        ),
        "wns_hold_ns": (
            "measured" if wns_hold is not None else "unavailable"
        ),
        **{
            key: "measured" if value is not None else "unavailable"
            for key, value in congestion_metrics.items()
        },
        **{
            key: "measured" if value is not None else "unavailable"
            for key, value in initial_congestion_metrics.items()
        },
        "congestion_status": (
            "measured" if congestion_status is not None else "unavailable"
        ),
        "initial_congestion_status": (
            "measured"
            if initial_congestion_status is not None
            else "unavailable"
        ),
        "wirelength_um": (
            "measured"
            if route["wirelength_um"] is not None
            else "unavailable"
        ),
        "via_count": (
            "measured"
            if route["via_count"] is not None
            else "unavailable"
        ),
        "final_detailed_route_violations": (
            "measured"
            if route["final_detailed_route_violations"] is not None
            else "unavailable"
        ),
        "design_area_um2": (
            "measured"
            if physical["design_area_um2"] is not None
            else "unavailable"
        ),
        "effective_place_density": (
            "measured"
            if density_measurement["effective_place_density"] is not None
            else "unavailable"
        ),
    }

    return {
        "final_def_available": def_path.is_file(),
        "final_odb_available": odb_path.is_file(),
        "final_def_nonempty": bool(def_text.strip()),
        "pre_drt_def_available": pre_drt_def_path.is_file(),
        "pre_drt_def_nonempty": bool(pre_drt_def_text.strip()),
        "placement_def_source": (
            "final.def"
            if def_text.strip()
            else "pre_drt.def"
            if pre_drt_def_text.strip()
            else None
        ),
        "artifacts_present": def_ok and odb_ok,
        "detailed_route_success_marker_found": route_success,
        **def_counts,
        **route,
        "effective_place_density": (
            density_measurement["effective_place_density"]
        ),
        "placed_cell_area_from_def_um2": (
            density_measurement.get("placed_cell_area_from_def_um2")
        ),
        "placement_row_area_um2": (
            density_measurement.get("placement_row_area_um2")
        ),
        "effective_place_density_method": (
            density_measurement.get("effective_place_density_method")
        ),
        "design_area_um2": physical["design_area_um2"],
        "wns_setup_ns": wns_setup,
        "tns_setup_ns": tns_setup,
        "wns_hold_ns": wns_hold,
        **congestion_metrics,
        **initial_congestion_metrics,
        "congestion_status": congestion_status,
        "initial_congestion_status": initial_congestion_status,
        "setup_report_source": (
            str(setup_path)
            if setup_path
            else "logs" if setup_src
            else None
        ),
        "tns_report_source": (
            str(tns_path)
            if tns_path
            else "logs" if tns_src
            else None
        ),
        "hold_report_source": (
            str(hold_path)
            if hold_path
            else "logs" if hold_src
            else None
        ),
        "congestion_report_source": congestion_source,
        "initial_congestion_report_source": (
            initial_congestion_source
        ),
        "routing_log_source": route_name,
        "log_files_found": list(logs),
        "target_measurement_status": measurements,
        "note": (
            "DEF route counts are approximate. Cell classes are heuristic "
            "and should be validated against Liberty. Fanout uses Yosys "
            "directions with Liberty fallback. Congestion is extracted only "
            "from a dedicated global-routing report/log. Resource, demand, "
            "usage and congestion are physical targets, not predictors. "
            "Effective density is measured from placed LEF macro area "
            "divided by DEF placement-row area when geometry is available. "
            "Router violations are not independent signoff DRC."
        ),
        "target_feature_stage": (
            "post-placement/routing targets; exclude these outcomes "
            "from predictors when predicting the same outcomes"
        ),
    }


# ---------------------------------------------------------------------------
# Run status and configuration
# ---------------------------------------------------------------------------

def read_tcl_status(
    run_dir: Path,
) -> tuple[str | None, str | None]:
    """Read the final Tcl status-history row if the flow generated one."""
    path = run_dir / "fir_features_status.csv"

    if not path.is_file():
        return None, None

    try:
        with path.open(
            "r",
            newline="",
            encoding="utf-8-sig",
        ) as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeError):
        return None, None

    if not rows:
        return None, None

    row = rows[-1]

    status = (
        row.get("status")
        or row.get("flow_status")
        or ""
    ).strip().upper()

    stage = (
        row.get("last_completed_stage")
        or row.get("stage")
        or ""
    ).strip()

    if not status and not stage:
        return None, None

    if status in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
        return "completed", stage or "flow_completed"

    if status in {"FAILED", "ERROR"}:
        return "failed", stage or "unknown"

    if status in {"IN_PROGRESS", "RUNNING", "STARTED"}:
        return "in_progress", stage or "unknown"

    return (
        status.lower() if status else None,
        stage or None,
    )


def validate_configuration(
    args: argparse.Namespace,
    run_dir: Path,
    netlist_path: Path,
) -> dict[str, Any] | None:
    """Validate CLI metadata against configuration.json."""
    manifest_path = run_dir / "configuration.json"

    if not manifest_path.is_file():
        print(
            f"WARNING: configuration manifest not found: {manifest_path}",
            file=sys.stderr,
        )
        return None

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Cannot read configuration manifest "
            f"{manifest_path}: {exc}"
        ) from exc

    if not isinstance(manifest, dict):
        raise ValueError(
            "configuration.json must contain a JSON object."
        )

    manifest_mode = str(
        first_manifest_value(
            manifest,
            ("floorplan_mode",),
            args.floorplan_mode or "utilization",
        )
    ).strip().lower()

    if manifest_mode not in {"fixed", "utilization"}:
        raise ValueError(
            f"Unsupported floorplan_mode in configuration.json: "
            f"{manifest_mode!r}"
        )

    if args.floorplan_mode and args.floorplan_mode != manifest_mode:
        raise ValueError(
            f"Floorplan mode mismatch: CLI supplied "
            f"{args.floorplan_mode!r}, but configuration.json contains "
            f"{manifest_mode!r}."
        )

    expected = {
        "run_id": args.run_id or run_dir.name,
        "design_id": args.design_id,
        "top": args.top,
        "taps": args.taps,
        "data_width": args.data_width,
        "coeff_width": args.coeff_width,
        "acc_width": args.acc_width,
        "place_density": args.place_density,
        "clock_period_ns": args.clock_period,
    }

    if manifest_mode == "utilization" and args.utilization is not None:
        expected["utilization"] = args.utilization

    for key, supplied in expected.items():
        if key not in manifest or supplied is None:
            continue

        actual = manifest[key]

        if (
            isinstance(supplied, (int, float))
            and not isinstance(supplied, bool)
        ):
            try:
                matches = (
                    abs(float(actual) - float(supplied)) <= 1e-9
                )
            except (TypeError, ValueError):
                matches = False
        else:
            matches = str(actual) == str(supplied)

        if not matches:
            raise ValueError(
                f"Configuration mismatch for '{key}': "
                f"CLI supplied {supplied!r}, "
                f"but configuration.json contains {actual!r}."
            )

    manifest_netlist = manifest.get("netlist_json")

    if manifest_netlist:
        recorded = Path(str(manifest_netlist))

        if not recorded.is_absolute():
            recorded = Path.cwd() / recorded

        supplied_path = (
            netlist_path
            if netlist_path.is_absolute()
            else Path.cwd() / netlist_path
        )

        if recorded.resolve() != supplied_path.resolve():
            raise ValueError(
                f"Netlist path mismatch: CLI supplied "
                f"'{netlist_path}', manifest records "
                f"'{manifest_netlist}'."
            )

    return manifest


def extract_run_conditions(
    args: argparse.Namespace,
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract pre-placement run conditions and floorplan geometry.

    Supports both fixed-area and utilization-derived floorplans.
    Requested utilization is null for fixed-area runs.
    """
    manifest = manifest or {}

    mode = str(
        first_manifest_value(
            manifest,
            ("floorplan_mode",),
            args.floorplan_mode or "utilization",
        )
    ).strip().lower()

    if mode not in {"fixed", "utilization"}:
        raise ValueError(
            f"Unsupported floorplan mode: {mode!r}"
        )

    if mode == "fixed":
        requested_utilization = None
        floorplan_utilization = None
        core_area_scale = None
    else:
        requested_utilization = manifest_number(
            manifest,
            ("requested_utilization_percent", "utilization"),
            args.utilization,
        )

        floorplan_utilization = manifest_number(
            manifest,
            (
                "floorplan_utilization_percent",
                "floorplan_util",
            ),
        )

        core_area_scale = manifest_number(
            manifest,
            ("core_area_scale",),
        )

    die_width = manifest_number(
        manifest,
        ("die_width_um", "die_width"),
    )

    die_height = manifest_number(
        manifest,
        ("die_height_um", "die_height"),
    )

    core_ll_x = manifest_number(
        manifest,
        ("core_ll_x_um", "core_ll_x"),
    )

    core_ll_y = manifest_number(
        manifest,
        ("core_ll_y_um", "core_ll_y"),
    )

    core_ur_x = manifest_number(
        manifest,
        ("core_ur_x_um", "core_ur_x"),
    )

    core_ur_y = manifest_number(
        manifest,
        ("core_ur_y_um", "core_ur_y"),
    )

    core_width = (
        core_ur_x - core_ll_x
        if (
            core_ur_x is not None
            and core_ll_x is not None
            and core_ur_x > core_ll_x
        )
        else None
    )

    core_height = (
        core_ur_y - core_ll_y
        if (
            core_ur_y is not None
            and core_ll_y is not None
            and core_ur_y > core_ll_y
        )
        else None
    )

    calculated_core_area = (
        core_width * core_height
        if core_width is not None and core_height is not None
        else None
    )

    recorded_core_area = manifest_number(
        manifest,
        ("core_area_um2",),
    )

    if recorded_core_area is None:
        recorded_core_area = calculated_core_area

    return {
        "run_id": args.run_id or "",
        "design_id": args.design_id,
        "floorplan_mode": mode,
        "requested_utilization_percent": requested_utilization,
        "floorplan_utilization_percent": floorplan_utilization,
        "core_area_scale": core_area_scale,
        "place_density_target": args.place_density,
        "clock_period_ns": args.clock_period,
        "die_width_um": die_width,
        "die_height_um": die_height,
        "die_area_um2": (
            die_width * die_height
            if die_width is not None and die_height is not None
            else None
        ),
        "core_ll_x_um": core_ll_x,
        "core_ll_y_um": core_ll_y,
        "core_ur_x_um": core_ur_x,
        "core_ur_y_um": core_ur_y,
        "core_width_um": core_width,
        "core_height_um": core_height,
        "core_area_um2": recorded_core_area,
        "fixed_place_density_target": (
            args.place_density if mode == "fixed" else None
        ),
        "configuration_manifest_found": manifest is not None,
    }


def model_input_features(
    pre: dict[str, Any],
) -> dict[str, int | float]:
    """Return numeric synthesized-netlist predictors only.

    Run conditions are stored separately.
    RTL parameters, design identity, categorical diagnostics and
    physical-design outcomes are not included here.
    """
    excluded = {
        "design_id",
        "top_module",
        "netlist_json",
        "feature_stage",
        "cell_classification_method",
        "fanout_method",
        "fanout_quality_warning",
        "fanout_direction_metadata_found",
        "fanout_metadata_complete_for_all_cells",
        "unique_cell_types",
        "utilization_constraint_percent",
        "clock_period_ns",
    }

    result: dict[str, int | float] = {}

    for key, value in pre.items():
        if key in excluded or isinstance(value, bool):
            continue

        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            result[key] = value

    return result


# ---------------------------------------------------------------------------
# Configuration and flow-status CSV
# ---------------------------------------------------------------------------

def write_configuration_csv(
    outdir: Path,
    run_dir: Path,
    args: argparse.Namespace,
    post: dict[str, Any],
    conditions: dict[str, Any],
) -> None:
    """Write FIR metadata and detected flow status."""
    run_id = args.run_id or run_dir.name

    def_ok = bool(
        post["final_def_available"]
        and post["final_def_nonempty"]
    )

    odb_ok = bool(post["final_odb_available"])
    route_success = bool(
        post["detailed_route_success_marker_found"]
    )

    tcl_status, tcl_stage = read_tcl_status(run_dir)

    if tcl_status == "completed" and def_ok and odb_ok:
        flow_status, last_stage = (
            "completed",
            tcl_stage or "flow_completed",
        )

    elif tcl_status == "failed":
        flow_status, last_stage = (
            "failed",
            tcl_stage or "unknown",
        )

    elif tcl_status == "in_progress":
        flow_status, last_stage = (
            "in_progress",
            tcl_stage or "unknown",
        )

    elif route_success and def_ok and odb_ok:
        flow_status, last_stage = (
            "detailed_routing_completed",
            "detailed_routing",
        )

    elif def_ok and odb_ok:
        flow_status, last_stage = (
            "artifacts_present_unverified",
            "unknown",
        )

    elif def_ok or odb_ok:
        flow_status, last_stage = (
            "partial_artifacts",
            "unknown",
        )

    elif post.get("log_files_found"):
        flow_status, last_stage = (
            "flow_incomplete_or_unverified",
            "unknown",
        )

    else:
        flow_status, last_stage = (
            "no_artifacts_or_logs",
            "unknown",
        )

    fields = [
        "run_id",
        "design_id",
        "floorplan_mode",
        "taps",
        "data_width",
        "coeff_width",
        "acc_width",
        "requested_utilization_percent",
        "floorplan_utilization_percent",
        "core_area_scale",
        "place_density_target",
        "clock_period_ns",
        "core_area_um2",
        "die_width_um",
        "die_height_um",
        "core_ll_x_um",
        "core_ll_y_um",
        "core_ur_x_um",
        "core_ur_y_um",
        "flow_status",
        "last_completed_stage",
    ]

    row = {
        "run_id": run_id,
        "design_id": args.design_id,
        "floorplan_mode": conditions["floorplan_mode"],
        "taps": args.taps,
        "data_width": args.data_width,
        "coeff_width": args.coeff_width,
        "acc_width": args.acc_width,
        "requested_utilization_percent": (
            conditions["requested_utilization_percent"]
        ),
        "floorplan_utilization_percent": (
            conditions["floorplan_utilization_percent"]
        ),
        "core_area_scale": conditions["core_area_scale"],
        "place_density_target": conditions["place_density_target"],
        "clock_period_ns": conditions["clock_period_ns"],
        "core_area_um2": conditions["core_area_um2"],
        "die_width_um": conditions["die_width_um"],
        "die_height_um": conditions["die_height_um"],
        "core_ll_x_um": conditions["core_ll_x_um"],
        "core_ll_y_um": conditions["core_ll_y_um"],
        "core_ur_x_um": conditions["core_ur_x_um"],
        "core_ur_y_um": conditions["core_ur_y_um"],
        "flow_status": flow_status,
        "last_completed_stage": last_stage,
    }

    write_rows_csv(
        outdir / "fir_features.csv",
        fields,
        row,
    )

    print(
        f"Detected flow status: {flow_status}\n"
        f"Last completed stage: {last_stage}\n"
        f"Routing success marker: {route_success}\n"
        f"Final DEF nonempty: {def_ok}\n"
        f"Final ODB nonempty: {odb_ok}"
    )


# ---------------------------------------------------------------------------
# Human-readable reporting
# ---------------------------------------------------------------------------

DISPLAY_NAMES = {
    "design_id": "Design ID",
    "top_module": "Top module",
    "netlist_json": "Netlist JSON",
    "cell_count": "Cell count",
    "netname_count": "Net names",
    "port_count": "Port count",
    "port_bit_count": "Port bits",
    "sequential_cell_count": "Sequential cells",
    "combinational_cell_count": "Combinational cells",
    "buffer_inverter_count": "Buffers / inverters",
    "other_cell_count": "Other cells",
    "unique_cell_type_count": "Unique cell types",
    "avg_bit_fanout": "Average bit fanout",
    "max_bit_fanout": "Maximum bit fanout",
    "std_bit_fanout": "Bit fanout standard deviation",
    "fanout_direction_metadata_found": "Fanout direction metadata found",
    "fanout_method": "Fanout extraction method",
    "fanout_quality_warning": "Fanout quality warning",
    "fanout_direction_coverage_fraction": "Fanout direction coverage fraction",
    "fanout_direction_coverage_percent": "Fanout direction coverage (%)",
    "fanout_metadata_complete_for_all_cells": "Fanout metadata complete",
    "fanout_cells_with_any_directions": "Cells with direction metadata",
    "fanout_cells_with_complete_directions": "Cells with complete direction metadata",
    "fanout_total_cells": "Connected cells checked for fanout",
    "total_net_bits": "Total net bits",
    "feature_stage": "Feature stage",
    "cell_classification_method": "Cell classification method",
    "final_def_available": "Final DEF available",
    "final_odb_available": "Final ODB available",
    "final_def_nonempty": "Final DEF non-empty",
    "artifacts_present": "Final DEF and ODB present",
    "detailed_route_success_marker_found": "Detailed routing success marker found",
    "def_nets_section_found": "DEF NETS section found",
    "routed_net_count": "Routed nets (approx.)",
    "route_segment_count": "Route segments (approx.)",
    "wirelength_um": "Wirelength (µm)",
    "via_count": "Via count",
    "final_detailed_route_violations": "Detailed-route violations (reported)",
    "effective_place_density": "Effective placement density",
    "effective_place_density_method": "Effective density measurement method",
    "placed_cell_area_from_def_um2": "Placed cell area from DEF (µm²)",
    "placement_row_area_um2": "Placement row area (µm²)",
    "design_area_um2": "Design area (µm²)",
    "wns_setup_ns": "Setup WNS (ns)",
    "tns_setup_ns": "Setup TNS (ns)",
    "wns_hold_ns": "Hold WNS (ns)",
    "grt_total_resource": "GRT total routing resources",
    "grt_total_demand": "GRT total routing demand",
    "grt_usage_percent": "GRT total resource usage (%)",
    "grt_max_horizontal_congestion": "GRT maximum horizontal congestion",
    "grt_max_vertical_congestion": "GRT maximum vertical congestion",
    "grt_total_congestion": "GRT total congestion",
    "congestion_status": "Final global-route congestion status",
    "initial_congestion_status": "Initial global-route congestion status",
    "initial_grt_total_resource": "Initial GRT total routing resources",
    "initial_grt_total_demand": "Initial GRT total routing demand",
    "initial_grt_usage_percent": "Initial GRT total resource usage (%)",
    "initial_grt_max_horizontal_congestion": "Initial GRT maximum horizontal congestion",
    "initial_grt_max_vertical_congestion": "Initial GRT maximum vertical congestion",
    "initial_grt_total_congestion": "Initial GRT total congestion",
    "setup_report_source": "Setup timing report source",
    "tns_report_source": "TNS report source",
    "hold_report_source": "Hold timing report source",
    "congestion_report_source": "Final congestion summary source",
    "initial_congestion_report_source": "Initial congestion summary source",
    "routing_log_source": "Routing log source",
    "note": "Notes / limitations",
    "target_feature_stage": "Target stage / ML leakage warning",
    "run_id": "Run ID",
    "floorplan_mode": "Floorplan mode",
    "requested_utilization_percent": "Requested utilization (%)",
    "floorplan_utilization_percent": "Floorplan utilization (%)",
    "core_area_scale": "Core area scale",
    "place_density_target": "Placement density target",
    "fixed_place_density_target": "Fixed-mode placement density target",
    "clock_period_ns": "Clock period (ns)",
    "die_width_um": "Die width (µm)",
    "die_height_um": "Die height (µm)",
    "die_area_um2": "Die area (µm²)",
    "core_ll_x_um": "Core lower-left X (µm)",
    "core_ll_y_um": "Core lower-left Y (µm)",
    "core_ur_x_um": "Core upper-right X (µm)",
    "core_ur_y_um": "Core upper-right Y (µm)",
    "core_width_um": "Core width (µm)",
    "core_height_um": "Core height (µm)",
    "core_area_um2": "Core area (µm²)",
    "taps": "FIR taps",
    "data_width": "Data width (bits)",
    "coeff_width": "Coefficient width (bits)",
    "acc_width": "Accumulator width (bits)",
    "flow_status": "Flow status",
    "last_completed_stage": "Last completed stage",
    "configuration_manifest_found": "Configuration manifest found",
}

for _layer in ("li1", "met1", "met2", "met3", "met4", "met5"):
    DISPLAY_NAMES.update({
        f"grt_{_layer}_resource": f"{_layer} routing resources",
        f"grt_{_layer}_demand": f"{_layer} routing demand",
        f"grt_{_layer}_usage_percent": f"{_layer} resource usage (%)",
        f"grt_{_layer}_max_horizontal_congestion": (
            f"{_layer} max horizontal congestion"
        ),
        f"grt_{_layer}_max_vertical_congestion": (
            f"{_layer} max vertical congestion"
        ),
        f"grt_{_layer}_total_congestion": (
            f"{_layer} total congestion"
        ),
        f"initial_grt_{_layer}_resource": (
            f"Initial {_layer} routing resources"
        ),
        f"initial_grt_{_layer}_demand": (
            f"Initial {_layer} routing demand"
        ),
        f"initial_grt_{_layer}_usage_percent": (
            f"Initial {_layer} resource usage (%)"
        ),
        f"initial_grt_{_layer}_max_horizontal_congestion": (
            f"Initial {_layer} max horizontal congestion"
        ),
        f"initial_grt_{_layer}_max_vertical_congestion": (
            f"Initial {_layer} max vertical congestion"
        ),
        f"initial_grt_{_layer}_total_congestion": (
            f"Initial {_layer} total congestion"
        ),
    })


def display_value(value: Any) -> str:
    """Human-readable value; unavailable values are never represented as zero."""
    if value is None or value == "":
        return "N/A (not extracted)"

    if isinstance(value, bool):
        return "Yes" if value else "No"

    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")

    if isinstance(value, (dict, list, tuple)):
        if isinstance(value, dict):
            return "; ".join(
                f"{DISPLAY_NAMES.get(key, key.replace('_', ' ').title())}: "
                f"{display_value(item)}"
                for key, item in value.items()
            )

        return ", ".join(str(item) for item in value) if value else "N/A"

    return str(value)


def markdown_table(
    rows: list[tuple[str, Any]],
) -> str:
    lines = [
        "| Metric | Value |",
        "|---|---|",
    ]

    for label, value in rows:
        safe_label = (
            str(label)
            .replace("|", r"\|")
            .replace("\n", " ")
        )

        safe_value = (
            display_value(value)
            .replace("|", r"\|")
            .replace("\n", "; ")
        )

        lines.append(f"| {safe_label} | {safe_value} |")

    return "\n".join(lines)


def write_human_report(
    outdir: Path,
    pre: dict[str, Any],
    conditions: dict[str, Any],
    post: dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
) -> None:
    """Write a Markdown report separating features, conditions and targets."""
    run_id = args.run_id or run_dir.name

    config_rows = [
        ("Run ID", run_id),
        ("Design ID", args.design_id),
        ("Top module", pre.get("top_module", args.top)),
        ("Floorplan mode", conditions["floorplan_mode"]),
        ("FIR taps", args.taps),
        ("Data width (bits)", args.data_width),
        ("Coefficient width (bits)", args.coeff_width),
        ("Accumulator width (bits)", args.acc_width),
        (
            "Requested utilization (%)",
            conditions["requested_utilization_percent"],
        ),
        (
            "Floorplan utilization (%)",
            conditions["floorplan_utilization_percent"],
        ),
        ("Placement density target", conditions["place_density_target"]),
        ("Clock period (ns)", conditions["clock_period_ns"]),
        ("Core area (µm²)", conditions["core_area_um2"]),
        ("Die width (µm)", conditions["die_width_um"]),
        ("Die height (µm)", conditions["die_height_um"]),
    ]

    tcl_status, tcl_stage = read_tcl_status(run_dir)

    config_rows.extend([
        ("Flow status", tcl_status or "derived from artifacts/logs"),
        ("Last completed stage", tcl_stage or "derived from artifacts/logs"),
    ])

    if tcl_status == "completed" and post.get("artifacts_present"):
        flow_status = "completed"
    elif tcl_status == "failed":
        flow_status = "failed"
    elif (
        post.get("detailed_route_success_marker_found")
        and post.get("final_def_nonempty")
        and post.get("final_odb_available")
    ):
        flow_status = "detailed_routing_completed"
    elif post.get("artifacts_present"):
        flow_status = "artifacts_present_unverified"
    else:
        flow_status = "incomplete_or_unverified"

    physical_keys = [
        "final_def_available",
        "final_odb_available",
        "final_def_nonempty",
        "artifacts_present",
        "detailed_route_success_marker_found",
        "routed_net_count",
        "route_segment_count",
        "wirelength_um",
        "via_count",
        "final_detailed_route_violations",
        "effective_place_density",
        "effective_place_density_method",
        "placed_cell_area_from_def_um2",
        "placement_row_area_um2",
        "design_area_um2",
        "wns_setup_ns",
        "tns_setup_ns",
        "wns_hold_ns",
        *empty_grt_metrics().keys(),
        *[
            f"initial_{key}"
            for key in empty_grt_metrics().keys()
        ],
        "congestion_status",
        "initial_congestion_status",
    ]

    synthesis_keys = [
        "cell_count",
        "netname_count",
        "port_count",
        "port_bit_count",
        "sequential_cell_count",
        "combinational_cell_count",
        "buffer_inverter_count",
        "other_cell_count",
        "unique_cell_type_count",
        "avg_bit_fanout",
        "max_bit_fanout",
        "std_bit_fanout",
        "total_net_bits",
        "fanout_direction_metadata_found",
        "fanout_direction_coverage_percent",
        "fanout_metadata_complete_for_all_cells",
        "fanout_method",
        "fanout_quality_warning",
        "cell_classification_method",
    ]

    condition_keys = [
        "floorplan_mode",
        "requested_utilization_percent",
        "floorplan_utilization_percent",
        "core_area_scale",
        "place_density_target",
        "clock_period_ns",
        "die_width_um",
        "die_height_um",
        "die_area_um2",
        "core_ll_x_um",
        "core_ll_y_um",
        "core_ur_x_um",
        "core_ur_y_um",
        "core_width_um",
        "core_height_um",
        "core_area_um2",
    ]

    lines = [
        "# FIR Physical-Design Feature Report",
        "",
        f"**Run:** `{run_id}`  ",
        f"**Detected flow status:** `{flow_status}`  ",
        f"**Run directory:** `{run_dir}`",
        "",
        "## 1. Design configuration",
        "",
        markdown_table(config_rows),
        "",
        "## 2. Synthesized-netlist features",
        "",
        markdown_table([
            (
                DISPLAY_NAMES.get(key, key.replace("_", " ").title()),
                pre.get(key),
            )
            for key in synthesis_keys
        ]),
        "",
        "## 3. Pre-placement run conditions",
        "",
        markdown_table([
            (
                DISPLAY_NAMES.get(key, key.replace("_", " ").title()),
                conditions.get(key),
            )
            for key in condition_keys
        ]),
        "",
        "## 4. Physical-design targets",
        "",
        markdown_table([
            (
                DISPLAY_NAMES.get(key, key.replace("_", " ").title()),
                post.get(key),
            )
            for key in physical_keys
        ]),
        "",
        "## 5. Measurement sources and status",
        "",
        markdown_table([
            (
                DISPLAY_NAMES.get(key, key.replace("_", " ").title()),
                post.get(key),
            )
            for key in (
                "setup_report_source",
                "tns_report_source",
                "hold_report_source",
                "congestion_report_source",
                "initial_congestion_report_source",
                "routing_log_source",
                "target_feature_stage",
                "note",
            )
        ]),
        "",
        "## 6. Interpretation notes",
        "",
        "- N/A means the extractor did not find a trustworthy value; it does not mean zero.",
        "- Netlist predictors contain synthesized-netlist measurements only.",
        "- Run conditions are stored separately and can be combined deliberately during model training.",
        "- Requested utilization and placement density target are not achieved placement density.",
        "- Effective density is measured from placed LEF macro area divided by DEF placement-row area when geometry is available.",
        "- Routed-net and route-segment counts parsed from DEF are approximate.",
        "- Cell-type classifications are heuristic and should be validated against Liberty metadata.",
        "- Timing and routing results are physical-design targets, not predictor inputs.",
        "- Zero reported detailed-route violations does not by itself prove full signoff DRC cleanliness.",
        "",
        "## 7. Output files",
        "",
        "| File | Purpose |",
        "|---|---|",
        "| `preplacement_features.csv` | Synthesized-netlist predictors |",
        "| `netlist_features.csv` | Alias of synthesized-netlist predictors |",
        "| `run_conditions.csv` | Pre-placement run conditions |",
        "| `physical_targets.csv` | Physical-design targets |",
        "| `features.csv` | Combined inspection record; not predictor-only |",
        "| `fir_features.csv` | FIR configuration and flow status |",
        "| `fir_feature_report.xlsx` | Formatted Excel workbook, if available |",
        "| `extraction_status.json` | Extraction diagnostics |",
        "| `run_report.md` | Human-readable report |",
        "",
    ]

    (outdir / "run_report.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Excel workbook
# ---------------------------------------------------------------------------

def write_excel_workbook(
    outdir: Path,
    pre: dict[str, Any],
    conditions: dict[str, Any],
    post: dict[str, Any],
    combined: dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
) -> bool:
    """Create a formatted XLSX workbook."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (
            Font,
            PatternFill,
            Alignment,
            Border,
            Side,
        )
        from openpyxl.utils import get_column_letter

    except ImportError:
        print(
            "WARNING: openpyxl is not installed; XLSX not created. "
            "Install it with: python -m pip install openpyxl",
            file=sys.stderr,
        )
        return False

    workbook = Workbook()

    config_ws = workbook.active
    config_ws.title = "Configuration"

    config = {
        "Run ID": args.run_id or run_dir.name,
        "Design ID": args.design_id,
        "Top module": pre.get("top_module", args.top),
        "Netlist JSON": str(Path(args.netlist_json)),
        "Run directory": str(run_dir),
        "Floorplan mode": conditions["floorplan_mode"],
        "FIR taps": args.taps,
        "Data width (bits)": args.data_width,
        "Coefficient width (bits)": args.coeff_width,
        "Accumulator width (bits)": args.acc_width,
        "Requested utilization (%)": (
            conditions["requested_utilization_percent"]
        ),
        "Floorplan utilization (%)": (
            conditions["floorplan_utilization_percent"]
        ),
        "Placement density target": conditions["place_density_target"],
        "Clock period (ns)": conditions["clock_period_ns"],
        "Core area (µm²)": conditions["core_area_um2"],
        "Die width (µm)": conditions["die_width_um"],
        "Die height (µm)": conditions["die_height_um"],
    }

    tcl_status, _ = read_tcl_status(run_dir)

    config["Flow status"] = (
        "completed"
        if tcl_status == "completed" and post.get("artifacts_present")
        else "failed"
        if tcl_status == "failed"
        else "detailed_routing_completed"
        if (
            post.get("detailed_route_success_marker_found")
            and post.get("final_def_nonempty")
            and post.get("final_odb_available")
        )
        else "artifacts_present_unverified"
        if post.get("artifacts_present")
        else "incomplete_or_unverified"
    )

    def style_sheet(ws) -> None:
        fill = PatternFill("solid", fgColor="1F4E78")
        header_font = Font(color="FFFFFF", bold=True)
        thin_gray = Side(style="thin", color="D9E2F3")

        for cell in ws[1]:
            cell.fill = fill
            cell.font = header_font
            cell.alignment = Alignment(
                vertical="center",
                wrap_text=True,
            )

        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(
                    vertical="top",
                    wrap_text=True,
                )
                cell.border = Border(bottom=thin_gray)

        ws.row_dimensions[1].height = 24

        for col_cells in ws.columns:
            column_letter = get_column_letter(
                col_cells[0].column
            )

            max_length = max(
                (
                    len(str(cell.value))
                    if cell.value is not None
                    else 0
                )
                for cell in col_cells
            )

            ws.column_dimensions[column_letter].width = min(
                max(max_length + 2, 16),
                52,
            )

    def add_key_value_sheet(
        ws,
        mapping: dict[str, Any],
        first_column: str,
    ) -> None:
        ws.append([first_column, "Value"])

        for key, value in mapping.items():
            label = DISPLAY_NAMES.get(
                key,
                key.replace("_", " ").title(),
            )

            if value is None or value == "":
                value = "N/A (not extracted)"
            elif isinstance(value, (dict, list, tuple)):
                value = display_value(value)

            ws.append([label, value])

        style_sheet(ws)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    add_key_value_sheet(
        config_ws,
        config,
        "Parameter",
    )

    synthesis_ws = workbook.create_sheet("Synthesis Features")
    add_key_value_sheet(
        synthesis_ws,
        pre,
        "Feature",
    )

    conditions_ws = workbook.create_sheet("Run Conditions")
    add_key_value_sheet(
        conditions_ws,
        conditions,
        "Condition",
    )

    physical_ws = workbook.create_sheet("Physical Targets")
    add_key_value_sheet(
        physical_ws,
        post,
        "Metric",
    )

    combined_ws = workbook.create_sheet("Combined Data")
    add_key_value_sheet(
        combined_ws,
        combined,
        "Field",
    )

    for worksheet in workbook.worksheets:
        worksheet.sheet_view.showGridLines = False

    workbook.save(
        outdir / "fir_feature_report.xlsx"
    )

    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract FIR synthesized-netlist features, "
            "run conditions and OpenROAD physical targets."
        )
    )

    parser.add_argument(
        "--netlist-json",
        required=True,
        help="Unique Yosys JSON netlist for this configuration",
    )

    parser.add_argument(
        "--run-dir",
        required=True,
        help="OpenROAD run directory for this configuration",
    )

    parser.add_argument(
        "--outdir",
        default=None,
        help="Output directory; defaults to --run-dir",
    )

    parser.add_argument("--top", default="fir_filter")
    parser.add_argument("--design-id", default="fir_filter")
    parser.add_argument("--run-id", required=True)

    parser.add_argument("--taps", type=int, required=True)
    parser.add_argument("--data-width", type=int, required=True)
    parser.add_argument("--coeff-width", type=int, required=True)
    parser.add_argument("--acc-width", type=int, required=True)

    parser.add_argument(
        "--floorplan-mode",
        choices=["fixed", "utilization"],
        default=None,
        help=(
            "Floorplan mode. If omitted, the mode is read from "
            "configuration.json; otherwise defaults to utilization."
        ),
    )

    parser.add_argument(
        "--utilization",
        type=float,
        default=None,
        help=(
            "Requested utilization percentage. Optional in fixed mode."
        ),
    )

    parser.add_argument(
        "--place-density",
        type=float,
        required=True,
        help="Placement density target as a fraction, e.g. 0.70",
    )

    parser.add_argument(
        "--clock-period",
        type=float,
        required=True,
    )

    parser.add_argument(
        "--flow-status",
        default=None,
        help=(
            "Deprecated; flow status is detected from artifacts "
            "and status CSV."
        ),
    )

    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    outdir = Path(args.outdir) if args.outdir else run_dir
    netlist_path = Path(args.netlist_json)

    # Validate design-generation metadata.
    if args.taps < 4 or args.taps > 32 or args.taps % 4 != 0:
        parser.error(
            "--taps must be a multiple of 4 between 4 and 32."
        )

    if args.data_width not in {8, 16, 24, 32, 40, 48}:
        parser.error(
            "--data-width must be one of: 8, 16, 24, 32, 40, 48."
        )

    if args.coeff_width != 8:
        parser.error(
            "--coeff-width must be 8 for the current FIR coefficient table."
        )

    if args.acc_width != args.data_width + args.coeff_width + 8:
        parser.error(
            "--acc-width must equal data-width + coeff-width + 8 "
            "for the current FIR flow policy."
        )

    if args.utilization is not None:
        if not (0 < args.utilization <= 100):
            parser.error(
                "--utilization must be > 0 and <= 100."
            )

    if not (0 < args.place_density <= 1):
        parser.error(
            "--place-density must be > 0 and <= 1."
        )

    if args.clock_period <= 0:
        parser.error(
            "--clock-period must be positive."
        )

    if not args.run_id.strip():
        parser.error(
            "--run-id cannot be empty."
        )

    try:
        if not run_dir.is_dir():
            raise FileNotFoundError(
                f"Run directory not found: {run_dir}"
            )

        if not netlist_path.is_file():
            raise FileNotFoundError(
                f"Netlist JSON not found: {netlist_path}"
            )

        # Read the manifest first to determine whether utilization
        # is required and to validate the selected floorplan mode.
        manifest = validate_configuration(
            args,
            run_dir,
            netlist_path,
        )

        manifest_mode = str(
            first_manifest_value(
                manifest or {},
                ("floorplan_mode",),
                args.floorplan_mode or "utilization",
            )
        ).strip().lower()

        if manifest_mode == "utilization":
            if args.utilization is None:
                raise ValueError(
                    "Utilization floorplan mode requires --utilization "
                    "or a corresponding value in the CLI."
                )

        elif manifest_mode != "fixed":
            raise ValueError(
                f"Unsupported floorplan mode: {manifest_mode!r}"
            )

        outdir.mkdir(
            parents=True,
            exist_ok=True,
        )

        # 1. Synthesized-netlist features.
        pre = extract_netlist_features(
            netlist_path,
            args.top,
            args.design_id,
        )

        # 2. Pre-placement run conditions.
        conditions = extract_run_conditions(
            args,
            manifest,
        )

        # 3. Physical-design targets.
        post = physical_features(run_dir)

        # 4. Strictly netlist-only model predictor dataset.
        ml_inputs = model_input_features(pre)

        write_json(
            outdir / "preplacement_features.json",
            ml_inputs,
        )

        write_csv(
            outdir / "preplacement_features.csv",
            ml_inputs,
        )

        # Explicitly named copies for dataset clarity.
        write_json(
            outdir / "netlist_features.json",
            ml_inputs,
        )

        write_csv(
            outdir / "netlist_features.csv",
            ml_inputs,
        )

        # 5. Run conditions are independent of netlist features.
        write_json(
            outdir / "run_conditions.json",
            conditions,
        )

        write_csv(
            outdir / "run_conditions.csv",
            conditions,
        )

        # 6. Physical targets are never included in predictors.
        write_json(
            outdir / "physical_targets.json",
            post,
        )

        write_csv(
            outdir / "physical_targets.csv",
            post,
        )

        # 7. Combined record for inspection only.
        combined = {
            **pre,
            **conditions,
            **post,
        }

        write_json(
            outdir / "features.json",
            combined,
        )

        write_csv(
            outdir / "features.csv",
            combined,
        )

        # 8. Configuration and flow-status record.
        write_configuration_csv(
            outdir,
            run_dir,
            args,
            post,
            conditions,
        )

        # 9. Human-readable report.
        write_human_report(
            outdir,
            pre,
            conditions,
            post,
            args,
            run_dir,
        )

        # 10. Excel workbook.
        xlsx_created = write_excel_workbook(
            outdir,
            pre,
            conditions,
            post,
            combined,
            args,
            run_dir,
        )

        # 11. Extraction status and provenance.
        status = {
            "extraction_success": True,
            "design_id": args.design_id,
            "run_id": args.run_id,
            "top_module": pre.get("top_module"),
            "netlist_json": str(netlist_path),
            "run_dir": str(run_dir),
            "output_dir": str(outdir),
            "floorplan_mode": conditions["floorplan_mode"],
            "configuration_manifest_found": manifest is not None,
            "netlist_model_input_feature_names": list(ml_inputs),
            "run_condition_feature_names": list(conditions),
            "model_input_policy": (
                "Netlist predictors contain numeric synthesized-netlist "
                "features only. Run conditions are stored separately. "
                "Physical placement/routing outcomes are targets, not "
                "predictors."
            ),
            "fanout_direction_metadata_found": (
                pre["fanout_direction_metadata_found"]
            ),
            "fanout_metadata_complete_for_all_cells": (
                pre["fanout_metadata_complete_for_all_cells"]
            ),
            "final_def_available": post["final_def_available"],
            "final_odb_available": post["final_odb_available"],
            "artifacts_present": post["artifacts_present"],
            "detailed_route_success_marker_found": (
                post["detailed_route_success_marker_found"]
            ),
            "target_measurement_status": (
                post["target_measurement_status"]
            ),
            "markdown_report_created": True,
            "excel_workbook_created": xlsx_created,
        }

        write_json(
            outdir / "extraction_status.json",
            status,
        )

    except (OSError, ValueError, KeyError) as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        return 1

    print("\n===== FIR Feature Extraction =====")
    print(f"Run ID          : {args.run_id}")
    print(f"Run directory   : {run_dir}")
    print(f"Output directory: {outdir}")
    print(f"Floorplan mode  : {conditions['floorplan_mode']}")
    print(f"Top module      : {pre['top_module']}")
    print(f"Cell count      : {pre['cell_count']}")
    print(f"Sequential      : {pre['sequential_cell_count']}")
    print(f"Combinational   : {pre['combinational_cell_count']}")
    print(f"Fanout method   : {pre['fanout_method']}")
    print(f"Final DEF       : {post['final_def_available']}")
    print(f"Final ODB       : {post['final_odb_available']}")
    print(
        "Routing success marker: "
        f"{post['detailed_route_success_marker_found']}"
    )
    print(
        "Initial GRT congestion status: "
        f"{post.get('initial_congestion_status')}"
    )
    print(
        "Final GRT congestion status: "
        f"{post.get('congestion_status')}"
    )

    print("\n===== Run Conditions =====")

    for key, value in conditions.items():
        print(f"{key}: {value}")

    print("\n===== Physical Targets =====")

    for key in (
        "wns_setup_ns",
        "tns_setup_ns",
        "wns_hold_ns",
        "initial_grt_total_resource",
        "initial_grt_total_demand",
        "initial_grt_usage_percent",
        "initial_grt_max_horizontal_congestion",
        "initial_grt_max_vertical_congestion",
        "initial_grt_total_congestion",
        "grt_total_resource",
        "grt_total_demand",
        "grt_usage_percent",
        "grt_max_horizontal_congestion",
        "grt_max_vertical_congestion",
        "grt_total_congestion",
        "wirelength_um",
        "via_count",
        "final_detailed_route_violations",
        "design_area_um2",
        "effective_place_density",
    ):
        print(f"{key}: {post.get(key)}")

    print("\n===== Target Measurement Status =====")

    for key, value in post["target_measurement_status"].items():
        print(f"{key}: {value}")

    print("\nGenerated:")
    print("  preplacement_features.csv/.json")
    print("  netlist_features.csv/.json")
    print("  run_conditions.csv/.json")
    print("  physical_targets.csv/.json")
    print("  features.csv/.json")
    print("  fir_features.csv")
    print("  run_report.md")
    print("  extraction_status.json")

    print(
        "Excel workbook: "
        + (
            "fir_feature_report.xlsx"
            if xlsx_created
            else "not created (openpyxl unavailable)"
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
