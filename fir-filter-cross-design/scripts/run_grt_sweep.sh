#!/usr/bin/env bash
# Capacity sweep on one saved placement.
#
# Re-runs ONLY global routing on RUN_DIR/pre_grt.odb for several values of
# global-routing capacity derating (GRT_ADJ). Placement, repair and timing are
# not repeated, so the placement is identical for every point and only the
# routing resources change.
#
# Usage:
#   scripts/run_grt_sweep.sh RUN_DIR [ADJ ...]
#   CONG_ITERS=0 scripts/run_grt_sweep.sh fir_project/runs/<run> 0 0.5 0.8 0.9
#
# Environment:
#   CONG_ITERS   rip-up/reroute iterations (default 0 = raw overflow,
#                without the router repairing congestion)
#
# Output:
#   RUN_DIR/grt_sweep/adj_<value>.log   full GRT log per point
#   RUN_DIR/grt_sweep_summary.csv       one parsed row per point

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

RUN_DIR="${1:?usage: scripts/run_grt_sweep.sh RUN_DIR [ADJ ...]}"
shift || true

ADJ_VALUES=("$@")

if (( ${#ADJ_VALUES[@]} == 0 )); then
    ADJ_VALUES=(0 0.3 0.5 0.65 0.8 0.9 0.95)
fi

CONG_ITERS="${CONG_ITERS:-0}"

[[ "$CONG_ITERS" =~ ^[0-9]+$ ]] || {
    echo "ERROR: CONG_ITERS must be a non-negative integer."
    exit 2
}

for tool in openroad python3 awk; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: Tool not found: $tool"
        exit 2
    }
done

ODB="$RUN_DIR/pre_grt.odb"

[[ -s "$ODB" ]] || {
    echo "ERROR: $ODB not found. Run the flow first (it writes pre_grt.odb)."
    exit 2
}

# Use the same routing layers as the production run.
read -r MIN_LAYER MAX_LAYER < <(python3 - "$RUN_DIR/configuration.json" <<'PY'
import json
import sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    data = {}

print(data.get("route_min_layer") or "met1",
      data.get("route_max_layer") or "met5")
PY
)

LAYERS="${MIN_LAYER}-${MAX_LAYER}"
SWEEP_DIR="$RUN_DIR/grt_sweep"
mkdir -p "$SWEEP_DIR"

echo "Sweep run     : $RUN_DIR"
echo "Layers        : $LAYERS"
echo "Rip-up iters  : $CONG_ITERS"
echo "Adjustments   : ${ADJ_VALUES[*]}"

for adj in "${ADJ_VALUES[@]}"; do

    awk -v a="$adj" \
        'BEGIN { exit !(a ~ /^[0-9]+([.][0-9]+)?$/ && a >= 0 && a < 1) }' || {
        echo "ERROR: adjustment '$adj' must satisfy 0 <= value < 1."
        exit 2
    }

    tag="$(awk -v a="$adj" 'BEGIN { printf "%g", a + 0 }')"
    tcl="$SWEEP_DIR/adj_${tag}.tcl"
    log="$SWEEP_DIR/adj_${tag}.log"

    {
        echo "read_db $ODB"
        echo "puts \"SWEEP_PARAMS: adj=${tag} congestion_iterations=${CONG_ITERS} layers=${LAYERS}\""
        echo "set_routing_layers -signal ${LAYERS} -clock ${LAYERS}"

        if awk -v a="$tag" 'BEGIN { exit !(a > 0) }'; then
            echo "set_global_routing_layer_adjustment ${LAYERS} ${tag}"
        fi

        echo "global_route -congestion_iterations ${CONG_ITERS} -allow_congestion -verbose"
    } > "$tcl"

    echo "--- adj=${tag}"

    if ! openroad -exit "$tcl" > "$log" 2>&1; then
        echo "WARNING: OpenROAD returned non-zero for adj=${tag}; see $log"
    fi

done

# Parse every log into one CSV and print a short table.
python3 - "$RUN_DIR" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

import extract_fir_features as ex

run_dir = Path(sys.argv[1])
rows = ex.summarize_grt_sweep(run_dir, run_dir)

if not rows:
    raise SystemExit("No sweep logs found.")

def fmt(value, width):
    return ("N/A" if value is None else f"{value:g}" if isinstance(value, float) else str(value)).rjust(width)

print()
print(f"{'adj':>6} {'resource':>10} {'demand':>10} {'usage%':>8} "
      f"{'maxH':>6} {'maxV':>6} {'total_ovf':>10}  overflow")

for row in rows:
    flag = row["grt_has_overflow"]
    print(
        fmt(row["grt_layer_adjustment"], 6),
        fmt(row["grt_total_resource"], 10),
        fmt(row["grt_total_demand"], 10),
        fmt(row["grt_usage_percent"], 8),
        fmt(row["grt_max_horizontal_overflow"], 6),
        fmt(row["grt_max_vertical_overflow"], 6),
        fmt(row["grt_total_overflow"], 10),
        " ",
        "N/A" if flag is None else ("YES" if flag else "no"),
    )

print(f"\nWrote {run_dir / 'grt_sweep_summary.csv'}")
PY
