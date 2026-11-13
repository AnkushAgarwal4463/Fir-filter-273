
#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

source config/sky130_example.env

: "${LIBERTY:?LIBERTY is not configured}"
: "${TECH_LEF:?TECH_LEF is not configured}"
: "${CELL_LEF:?CELL_LEF is not configured}"

export TAPS="${TAPS:-8}"
export DATA_WIDTH="${DATA_WIDTH:-8}"
export COEFF_WIDTH="${COEFF_WIDTH:-8}"
export ACC_WIDTH="${ACC_WIDTH:-$((DATA_WIDTH + COEFF_WIDTH + 8))}"
export UTIL="${UTIL:-40}"
export CLK_PERIOD="${CLK_PERIOD:-${CLK_PERIOD_NS:-10.0}}"
export ENABLE_REPAIR_TIMING="${ENABLE_REPAIR_TIMING:-1}"
export CORE_AREA_SCALE="${CORE_AREA_SCALE:-0.7}"

# Floorplan mode
export FLOORPLAN_MODE="${FLOORPLAN_MODE:-utilization}"

# Fixed floorplan dimensions (microns)
export DIE_WIDTH="${DIE_WIDTH:-250}"
export DIE_HEIGHT="${DIE_HEIGHT:-250}"

export CORE_LL_X="${CORE_LL_X:-5}"
export CORE_LL_Y="${CORE_LL_Y:-5}"
export CORE_UR_X="${CORE_UR_X:-245}"
export CORE_UR_Y="${CORE_UR_Y:-245}"

# Global-placement target for fixed-area mode
export FIXED_PLACE_DENSITY="${FIXED_PLACE_DENSITY:-0.80}"

# ------------------------------------------------------------
# 1. Validate design and experiment parameters
# ------------------------------------------------------------

if ! [[ "$TAPS" =~ ^[0-9]+$ ]] || (( TAPS < 4 || TAPS > 32 || TAPS % 4 != 0 )); then
    echo "ERROR: TAPS must be a multiple of 4 between 4 and 32."
    exit 2
fi

case "$DATA_WIDTH" in
    8|16|24|32|40|48) ;;
    *) echo "ERROR: DATA_WIDTH must be 8, 16, 24, 32, 40, or 48."; exit 2 ;;
esac

[[ "$COEFF_WIDTH" == 8 ]] || {
    echo "ERROR: COEFF_WIDTH must be 8."
    exit 2
}

case "$UTIL" in
    20|30|40|50|60|70|80) ;;
    *) echo "ERROR: UTIL must be 20, 30, 40, 50, 60, 70, or 80."; exit 2 ;;
esac

[[ "$ACC_WIDTH" =~ ^[0-9]+$ ]] || {
    echo "ERROR: ACC_WIDTH must be an integer."
    exit 2
}

(( ACC_WIDTH == DATA_WIDTH + COEFF_WIDTH + 8 )) || {
    echo "ERROR: ACC_WIDTH must equal DATA_WIDTH + COEFF_WIDTH + 8."
    exit 2
}

[[ "$ENABLE_REPAIR_TIMING" == 0 || "$ENABLE_REPAIR_TIMING" == 1 ]] || {
    echo "ERROR: ENABLE_REPAIR_TIMING must be 0 or 1."
    exit 2
}

case "$CLK_PERIOD" in
    5|5.0|7.5|10|10.0) ;;
    *) echo "ERROR: CLK_PERIOD must be 5, 7.5, or 10 ns."; exit 2 ;;
esac

# ------------------------------------------------------------
# 2. Configure floorplan
# ------------------------------------------------------------

case "$FLOORPLAN_MODE" in

    utilization)
        # Existing utilization-based floorplanning.

        awk -v s="$CORE_AREA_SCALE" \
            'BEGIN { exit !(s >= 0.60 && s <= 1.00) }' || {
            echo "ERROR: CORE_AREA_SCALE must be between 0.60 and 1.00."
            exit 2
        }

        export FLOORPLAN_UTIL
        FLOORPLAN_UTIL="$(awk -v u="$UTIL" -v s="$CORE_AREA_SCALE" \
            'BEGIN { printf "%.4f", u / s }')"

        awk -v u="$FLOORPLAN_UTIL" \
            'BEGIN { exit !(u > 0 && u <= 80) }' || {
            echo "ERROR: Effective floorplan utilization must be > 0 and <= 80%."
            echo "UTIL=$UTIL CORE_AREA_SCALE=$CORE_AREA_SCALE gives FLOORPLAN_UTIL=$FLOORPLAN_UTIL."
            exit 2
        }

        export PLACE_DENSITY
        PLACE_DENSITY="$(awk -v u="$FLOORPLAN_UTIL" \
            'BEGIN { printf "%.4f", u / 100.0 }')"

        export CORE_AREA_UM2=""
        export FIXED_CORE_UTILIZATION=""

        ;;

    fixed)
        # Fixed die/core dimensions.

        for var in DIE_WIDTH DIE_HEIGHT CORE_LL_X CORE_LL_Y CORE_UR_X CORE_UR_Y FIXED_PLACE_DENSITY; do
            value="${!var}"

            awk -v v="$value" \
                'BEGIN {
                    exit !(v ~ /^[0-9]+([.][0-9]+)?$/ && v > 0)
                }' || {
                echo "ERROR: $var must be a positive numeric value."
                exit 2
            }
        done

        # Validate core coordinates against die dimensions.

        awk \
            -v dw="$DIE_WIDTH" \
            -v dh="$DIE_HEIGHT" \
            -v llx="$CORE_LL_X" \
            -v lly="$CORE_LL_Y" \
            -v urx="$CORE_UR_X" \
            -v ury="$CORE_UR_Y" \
            'BEGIN {
                exit !(
                    llx < urx &&
                    lly < ury &&
                    llx >= 0 &&
                    lly >= 0 &&
                    urx <= dw &&
                    ury <= dh
                )
            }' || {
                echo "ERROR: Core coordinates must define a positive area inside the die."
                exit 2
            }

        # Calculate core dimensions and area.

        export CORE_WIDTH_UM
        export CORE_HEIGHT_UM
        export CORE_AREA_UM2

        CORE_WIDTH_UM="$(awk -v x1="$CORE_LL_X" -v x2="$CORE_UR_X" \
            'BEGIN { printf "%.4f", x2-x1 }')"

        CORE_HEIGHT_UM="$(awk -v y1="$CORE_LL_Y" -v y2="$CORE_UR_Y" \
            'BEGIN { printf "%.4f", y2-y1 }')"

        CORE_AREA_UM2="$(awk -v w="$CORE_WIDTH_UM" -v h="$CORE_HEIGHT_UM" \
            'BEGIN { printf "%.4f", w*h }')"

        # Placement target is independently configurable in fixed mode.

        export PLACE_DENSITY="$FIXED_PLACE_DENSITY"

        awk -v d="$PLACE_DENSITY" \
            'BEGIN { exit !(d > 0 && d <= 1) }' || {
            echo "ERROR: FIXED_PLACE_DENSITY must be > 0 and <= 1."
            exit 2
        }

        # Calculate approximate utilization after synthesis.
        # This is filled later, after the netlist has been synthesized.

        export FIXED_CORE_UTILIZATION=""

        # Utilization-based parameters are not applicable.

        export FLOORPLAN_UTIL=""
        ;;

    *)
        echo "ERROR: FLOORPLAN_MODE must be utilization or fixed."
        exit 2
        ;;
esac

# ------------------------------------------------------------
# 3. Configure run identity
# ------------------------------------------------------------

export CLK_PERIOD_NS="$CLK_PERIOD"

CLK_TAG="$(awk -v c="$CLK_PERIOD" 'BEGIN { printf "%g", c + 0 }')"
CORE_AREA_TAG="$(awk -v s="$CORE_AREA_SCALE" 'BEGIN { printf "%g", s + 0 }')"

if [[ "$FLOORPLAN_MODE" == "fixed" ]]; then

    DIE_TAG="$(awk -v w="$DIE_WIDTH" -v h="$DIE_HEIGHT" \
        'BEGIN { printf "%gx%g", w, h }')"

    CORE_TAG="$(awk -v x1="$CORE_LL_X" -v y1="$CORE_LL_Y" \
        -v x2="$CORE_UR_X" -v y2="$CORE_UR_Y" \
        'BEGIN { printf "%gx%g", x2-x1, y2-y1 }')"

    RUN_TAG_BASE="fir_t${TAPS}_dw${DATA_WIDTH}_cw${COEFF_WIDTH}_aw${ACC_WIDTH}_fixed_die${DIE_TAG}_core${CORE_TAG}_pd${PLACE_DENSITY}_clk${CLK_TAG}"

else

    RUN_TAG_BASE="fir_t${TAPS}_dw${DATA_WIDTH}_cw${COEFF_WIDTH}_aw${ACC_WIDTH}_u${UTIL}_ca${CORE_AREA_TAG}_clk${CLK_TAG}"

fi

if [[ "$ENABLE_REPAIR_TIMING" == 0 ]]; then
    export RUN_TAG="${RUN_TAG_BASE}_tr0"
else
    export RUN_TAG="$RUN_TAG_BASE"
fi

[[ -z "${RUN_TAG_OVERRIDE:-}" ]] || {
    echo "ERROR: RUN_TAG_OVERRIDE is not supported."
    exit 2
}

export DESIGN_ID="fir_filter_t${TAPS}_dw${DATA_WIDTH}"

export RUN_DIR="fir_project/runs/${RUN_TAG}"

# Synthesis artifacts depend on RTL parameters, not floorplan dimensions.

SYNTH_TAG="t${TAPS}_dw${DATA_WIDTH}_cw${COEFF_WIDTH}_aw${ACC_WIDTH}"

export NETLIST="fir_project/synth/fir_filter_${SYNTH_TAG}.v"
export NETLIST_JSON="fir_project/synth/fir_filter_${SYNTH_TAG}.json"
export SYNTH_METADATA="fir_project/synth/fir_filter_${SYNTH_TAG}_metadata.json"

export SDC="fir_project/synth/fir_filter_${RUN_TAG}.sdc"

# ------------------------------------------------------------
# 4. Print experiment configuration
# ------------------------------------------------------------

printf '%s\n' \
    "TAPS=$TAPS" \
    "DATA_WIDTH=$DATA_WIDTH" \
    "COEFF_WIDTH=$COEFF_WIDTH" \
    "ACC_WIDTH=$ACC_WIDTH" \
    "FLOORPLAN_MODE=$FLOORPLAN_MODE" \
    "REQUESTED_UTIL=$UTIL%" \
    "CORE_AREA_SCALE=$CORE_AREA_SCALE" \
    "FLOORPLAN_UTIL=${FLOORPLAN_UTIL:-not_applicable}" \
    "PLACE_DENSITY=$PLACE_DENSITY" \
    "CLK_PERIOD=$CLK_PERIOD ns" \
    "ENABLE_REPAIR_TIMING=$ENABLE_REPAIR_TIMING" \
    "RUN_TAG=$RUN_TAG"

if [[ "$FLOORPLAN_MODE" == "fixed" ]]; then
    printf '%s\n' \
        "DIE=${DIE_WIDTH}x${DIE_HEIGHT} um" \
        "CORE=${CORE_WIDTH_UM}x${CORE_HEIGHT_UM} um" \
        "CORE_AREA=${CORE_AREA_UM2} um^2" \
        "CORE_LL=(${CORE_LL_X},${CORE_LL_Y})" \
        "CORE_UR=(${CORE_UR_X},${CORE_UR_Y})"
fi

# ------------------------------------------------------------
# 5. Check required tools and source files
# ------------------------------------------------------------

required_files=(
    "$LIBERTY"
    "$TECH_LEF"
    "$CELL_LEF"
    rtl/fir_filter.v
    scripts/synth_fir.ys
    scripts/flow_fir.tcl
    scripts/extract_fir_features.py
    constraints/fir_filter.sdc.in
)

for file in "${required_files[@]}"; do
    [[ -s "$file" ]] || {
        echo "ERROR: Required file missing/empty: $file"
        exit 2
    }
done

for tool in yosys openroad python3 awk; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ERROR: Tool not found: $tool"
        exit 2
    }
done

mkdir -p fir_project/synth

# Never overwrite an existing experiment.

if [[ -e "$RUN_DIR" ]]; then
    echo "ERROR: Run directory already exists: $RUN_DIR"
    echo "Use a new experiment configuration or archive the previous run."
    exit 2
fi

mkdir -p "$RUN_DIR"

rm -f "$SDC"

# ------------------------------------------------------------
# 6. Generate the Yosys script
# ------------------------------------------------------------

python3 - <<'PY'
import os
import re
from pathlib import Path

source = Path("scripts/synth_fir.ys").read_text(encoding="utf-8")

replacements = {
    "__DATA_WIDTH__": os.environ["DATA_WIDTH"],
    "__COEFF_WIDTH__": os.environ["COEFF_WIDTH"],
    "__TAPS__": os.environ["TAPS"],
    "__ACC_WIDTH__": os.environ["ACC_WIDTH"],
    "__LIB_FILE__": os.environ["LIBERTY"],
    "__NETLIST_JSON__": os.environ["NETLIST_JSON"],
    "__NETLIST_VERILOG__": os.environ["NETLIST"],
}

for token, value in replacements.items():
    source = source.replace(token, value)

remaining = sorted(set(re.findall(r"__[A-Z0-9_]+__", source)))
if remaining:
    raise SystemExit("Unresolved Yosys placeholders: " + ", ".join(remaining))

Path("fir_project/synth/synth_fir.generated.ys").write_text(
    source,
    encoding="utf-8"
)
PY

# ------------------------------------------------------------
# 7. Synthesize only when reusable netlist artifacts are absent
#    or synthesis metadata does not match.
# ------------------------------------------------------------

REUSE_NETLIST=0

if [[ -s "$NETLIST" && -s "$NETLIST_JSON" && -s "$SYNTH_METADATA" ]]; then

    if python3 - "$SYNTH_METADATA" "$LIBERTY" "$DESIGN_ID" <<'PY'
import json
import sys
from pathlib import Path

metadata = json.loads(Path(sys.argv[1]).read_text())

valid = (
    metadata.get("design_id") == sys.argv[3]
    and metadata.get("liberty") == str(Path(sys.argv[2]).resolve())
)

sys.exit(0 if valid else 1)
PY
    then
        REUSE_NETLIST=1
    fi
fi

if [[ "$REUSE_NETLIST" -eq 1 ]]; then

    echo "Reusing validated synthesized netlist."

else

    echo "Netlist missing or synthesis metadata mismatch. Running Yosys."

    rm -f "$NETLIST" "$NETLIST_JSON" "$SYNTH_METADATA"

    yosys -l "$RUN_DIR/synthesis.log" \
        -s fir_project/synth/synth_fir.generated.ys

    [[ -s "$NETLIST" && -s "$NETLIST_JSON" ]] || {
        echo "ERROR: Synthesis outputs missing."
        exit 3
    }

    python3 - "$SYNTH_METADATA" "$LIBERTY" "$DESIGN_ID" <<'PY'
import json
import sys
from pathlib import Path

metadata = {
    "design_id": sys.argv[3],
    "liberty": str(Path(sys.argv[2]).resolve())
}

Path(sys.argv[1]).write_text(json.dumps(metadata, indent=2) + "\n")
PY

fi

for file in "$NETLIST" "$NETLIST_JSON"; do
    [[ -s "$file" ]] || {
        echo "ERROR: Synthesis output missing: $file"
        exit 3
    }
done

python3 - "$NETLIST_JSON" <<'PY'
import json
import sys
from pathlib import Path

module = json.loads(Path(sys.argv[1]).read_text()).get(
    "modules", {}
).get("fir_filter")

if not module or not module.get("cells") or not module.get("ports"):
    raise SystemExit("Synthesized JSON lacks fir_filter cells or ports.")

print(
    f"Yosys JSON valid: {len(module['cells'])} cells; "
    f"{len(module['ports'])} ports"
)
PY

# ------------------------------------------------------------
# 8. Generate the run-specific SDC
# ------------------------------------------------------------

sed "s|__CLK_PERIOD__|${CLK_PERIOD}|g" \
    constraints/fir_filter.sdc.in > "$SDC"

grep -q '__CLK_PERIOD__' "$SDC" && {
    echo "ERROR: Unresolved SDC clock placeholder."
    exit 3
}

grep -E 'create_clock.*-period' "$SDC" || true

# ------------------------------------------------------------
# 9. Write configuration metadata
# ------------------------------------------------------------

python3 - "$RUN_DIR/configuration.json" <<'PY'
import json
import os
import sys
from pathlib import Path

period = float(os.environ["CLK_PERIOD"])
repair = os.environ["ENABLE_REPAIR_TIMING"] == "1"
mode = os.environ["FLOORPLAN_MODE"]

def optional_float(name):
    value = os.environ.get(name, "")
    return float(value) if value else None

data = {
    "design_id": os.environ["DESIGN_ID"],
    "run_id": os.environ["RUN_TAG"],
    "top": "fir_filter",

    "taps": int(os.environ["TAPS"]),
    "data_width": int(os.environ["DATA_WIDTH"]),
    "coeff_width": int(os.environ["COEFF_WIDTH"]),
    "acc_width": int(os.environ["ACC_WIDTH"]),

    "floorplan_mode": mode,

    "requested_utilization_percent": float(os.environ["UTIL"]),
    "floorplan_utilization_percent": optional_float("FLOORPLAN_UTIL"),
    "core_area_scale": (
        optional_float("CORE_AREA_SCALE")
        if mode == "utilization" else None
    ),

    "place_density": float(os.environ["PLACE_DENSITY"]),
    "fixed_place_density": (
        float(os.environ["FIXED_PLACE_DENSITY"])
        if mode == "fixed" else None
    ),

    "die_width_um": (
        float(os.environ["DIE_WIDTH"]) if mode == "fixed" else None
    ),
    "die_height_um": (
        float(os.environ["DIE_HEIGHT"]) if mode == "fixed" else None
    ),

    "core_ll_x_um": (
        float(os.environ["CORE_LL_X"]) if mode == "fixed" else None
    ),
    "core_ll_y_um": (
        float(os.environ["CORE_LL_Y"]) if mode == "fixed" else None
    ),
    "core_ur_x_um": (
        float(os.environ["CORE_UR_X"]) if mode == "fixed" else None
    ),
    "core_ur_y_um": (
        float(os.environ["CORE_UR_Y"]) if mode == "fixed" else None
    ),

    "core_width_um": optional_float("CORE_WIDTH_UM"),
    "core_height_um": optional_float("CORE_HEIGHT_UM"),
    "core_area_um2": optional_float("CORE_AREA_UM2"),

    "clock_period": period,
    "clock_period_ns": period,
    "frequency_mhz": 1000.0 / period,

    "enable_repair_timing": repair,
    "repair_timing_mode": "enabled" if repair else "disabled_diagnostic",

    "netlist_verilog": os.environ["NETLIST"],
    "netlist_json": os.environ["NETLIST_JSON"],
    "sdc_file": os.environ["SDC"],
}

Path(sys.argv[1]).write_text(
    json.dumps(data, indent=2) + "\n",
    encoding="utf-8"
)
PY

# ------------------------------------------------------------
# 10. Export variables for OpenROAD
# ------------------------------------------------------------

export NETLIST NETLIST_JSON SDC RUN_DIR UTIL FLOORPLAN_UTIL
export PLACE_DENSITY CORE_AREA_SCALE ENABLE_REPAIR_TIMING
export FLOORPLAN_MODE DIE_WIDTH DIE_HEIGHT
export CORE_LL_X CORE_LL_Y CORE_UR_X CORE_UR_Y
export CORE_AREA_UM2 CORE_WIDTH_UM CORE_HEIGHT_UM
export FIXED_PLACE_DENSITY

# ------------------------------------------------------------
# 11. Run OpenROAD
# ------------------------------------------------------------

OPENROAD_STATUS=0

if openroad -exit scripts/flow_fir.tcl 2>&1 | tee "$RUN_DIR/openroad_log.txt"; then
    echo "OpenROAD process returned success."
else
    OPENROAD_STATUS=$?
    echo "WARNING: OpenROAD returned exit code $OPENROAD_STATUS; attempting partial extraction."
fi

FINAL_DEF_PRESENT=0
FINAL_ODB_PRESENT=0
PRE_DRT_DEF_PRESENT=0
PRE_DRT_ODB_PRESENT=0

[[ -s "$RUN_DIR/final.def" ]] && FINAL_DEF_PRESENT=1
[[ -s "$RUN_DIR/final.odb" ]] && FINAL_ODB_PRESENT=1
[[ -s "$RUN_DIR/pre_drt.def" ]] && PRE_DRT_DEF_PRESENT=1
[[ -s "$RUN_DIR/pre_drt.odb" ]] && PRE_DRT_ODB_PRESENT=1

printf 'final.def=%s final.odb=%s pre_drt.def=%s pre_drt.odb=%s\n' \
    "$FINAL_DEF_PRESENT" "$FINAL_ODB_PRESENT" \
    "$PRE_DRT_DEF_PRESENT" "$PRE_DRT_ODB_PRESENT"

# ------------------------------------------------------------
# 12. Extract pre-placement features and physical targets
# ------------------------------------------------------------

EXTRACTION_STATUS=0

if python3 scripts/extract_fir_features.py \
    --netlist-json "$NETLIST_JSON" \
    --run-dir "$RUN_DIR" \
    --outdir "$RUN_DIR" \
    --top fir_filter \
    --design-id "$DESIGN_ID" \
    --run-id "$RUN_TAG" \
    --taps "$TAPS" \
    --data-width "$DATA_WIDTH" \
    --coeff-width "$COEFF_WIDTH" \
    --acc-width "$ACC_WIDTH" \
    --utilization "$UTIL" \
    --place-density "$PLACE_DENSITY" \
    --clock-period "$CLK_PERIOD"; then

    echo "Feature extraction completed."

else

    EXTRACTION_STATUS=$?
    echo "ERROR: Feature extraction failed with exit code $EXTRACTION_STATUS."

fi

required_outputs=(
    configuration.json
    preplacement_features.csv
    preplacement_features.json
    physical_targets.csv
    physical_targets.json
    features.csv
    features.json
    fir_features.csv
    extraction_status.json
    run_report.md
)

MISSING_OUTPUTS=0

for name in "${required_outputs[@]}"; do
    if [[ -s "$RUN_DIR/$name" ]]; then
        echo "FOUND: $RUN_DIR/$name"
    else
        echo "MISSING/EMPTY: $RUN_DIR/$name"
        MISSING_OUTPUTS=$((MISSING_OUTPUTS + 1))
    fi
done

(( MISSING_OUTPUTS == 0 )) || {
    echo "ERROR: $MISSING_OUTPUTS extraction output(s) missing."
    exit 5
}

(( EXTRACTION_STATUS == 0 )) || exit 5

(( OPENROAD_STATUS == 0 )) || {
    echo "ERROR: OpenROAD failed ($OPENROAD_STATUS); partial outputs preserved."
    exit 4
}

(( FINAL_DEF_PRESENT == 1 && FINAL_ODB_PRESENT == 1 )) || {
    echo "ERROR: OpenROAD succeeded but final DEF/ODB are incomplete."
    exit 4
}

echo "FIR flow completed successfully: $RUN_TAG"
