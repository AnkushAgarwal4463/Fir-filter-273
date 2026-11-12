#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
PLACE_DENSITY_OVERRIDE="${PLACE_DENSITY:-}"
REQUESTED_REPAIR_MODE="${ENABLE_REPAIR_TIMING:-}"
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
export ENABLE_REPAIR_TIMING="${REQUESTED_REPAIR_MODE:-${ENABLE_REPAIR_TIMING:-1}}"

if ! [[ "$TAPS" =~ ^[0-9]+$ ]] || (( TAPS < 4 || TAPS > 32 || TAPS % 4 != 0 )); then
    echo "ERROR: TAPS must be a multiple of 4 between 4 and 32."
    exit 2
fi
case "$DATA_WIDTH" in 8|16|24|32|40|48) ;; *) echo "ERROR: DATA_WIDTH must be 8, 16, 24, 32, 40, or 48."; exit 2 ;; esac
[[ "$COEFF_WIDTH" == 8 ]] || { echo "ERROR: COEFF_WIDTH must be 8."; exit 2; }
case "$UTIL" in 20|30|40|50|60|70|80) ;; *) echo "ERROR: UTIL must be 20, 30, 40, 50, 60, 70, or 80."; exit 2 ;; esac
[[ "$ACC_WIDTH" =~ ^[0-9]+$ ]] || { echo "ERROR: ACC_WIDTH must be an integer."; exit 2; }
(( ACC_WIDTH == DATA_WIDTH + COEFF_WIDTH + 8 )) || { echo "ERROR: ACC_WIDTH must equal DATA_WIDTH + COEFF_WIDTH + 8."; exit 2; }
[[ "$ENABLE_REPAIR_TIMING" == 0 || "$ENABLE_REPAIR_TIMING" == 1 ]] || { echo "ERROR: ENABLE_REPAIR_TIMING must be 0 or 1."; exit 2; }
case "$CLK_PERIOD" in 5|5.0|7.5|10|10.0) ;; *) echo "ERROR: CLK_PERIOD must be 5, 7.5, or 10 ns."; exit 2 ;; esac

if [[ -n "$PLACE_DENSITY_OVERRIDE" ]]; then
  export PLACE_DENSITY="$PLACE_DENSITY_OVERRIDE"
else
  export PLACE_DENSITY="$(awk -v u="$UTIL" 'BEGIN { printf "%.4f", u / 100 }')"
fi
awk -v d="$PLACE_DENSITY" 'BEGIN { exit !(d > 0 && d <= 1) }' || { echo "ERROR: PLACE_DENSITY must be > 0 and <= 1."; exit 2; }

export CLK_PERIOD_NS="$CLK_PERIOD"
CLK_TAG="$(awk -v c="$CLK_PERIOD" 'BEGIN { printf "%g", c + 0 }')"
RUN_TAG_BASE="fir_t${TAPS}_dw${DATA_WIDTH}_cw${COEFF_WIDTH}_aw${ACC_WIDTH}_u${UTIL}_clk${CLK_TAG}"
if [[ "$ENABLE_REPAIR_TIMING" == 0 ]]; then export RUN_TAG="${RUN_TAG_BASE}_tr0"; else export RUN_TAG="$RUN_TAG_BASE"; fi
[[ -z "${RUN_TAG_OVERRIDE:-}" ]] || { echo "ERROR: RUN_TAG_OVERRIDE is not supported."; exit 2; }
export DESIGN_ID="fir_filter_t${TAPS}_dw${DATA_WIDTH}"
export RUN_DIR="fir_project/runs/${RUN_TAG}"
export NETLIST="fir_project/synth/fir_filter_${RUN_TAG}.v"
export NETLIST_JSON="fir_project/synth/fir_filter_${RUN_TAG}.json"
export SDC="fir_project/synth/fir_filter_${RUN_TAG}.sdc"

printf 'TAPS=%s DATA_WIDTH=%s COEFF_WIDTH=%s ACC_WIDTH=%s UTIL=%s PLACE_DENSITY=%s CLK_PERIOD=%s ns ENABLE_REPAIR_TIMING=%s RUN_TAG=%s\n' \
  "$TAPS" "$DATA_WIDTH" "$COEFF_WIDTH" "$ACC_WIDTH" "$UTIL" "$PLACE_DENSITY" "$CLK_PERIOD" "$ENABLE_REPAIR_TIMING" "$RUN_TAG"

required_files=("$LIBERTY" "$TECH_LEF" "$CELL_LEF" rtl/fir_filter.v scripts/synth_fir.ys scripts/flow_fir.tcl scripts/extract_fir_features.py constraints/fir_filter.sdc.in)
for file in "${required_files[@]}"; do [[ -s "$file" ]] || { echo "ERROR: Required file missing/empty: $file"; exit 2; }; done
for tool in yosys openroad python3; do command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: Tool not found: $tool"; exit 2; }; done

mkdir -p fir_project/synth
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"
rm -f "$NETLIST" "$NETLIST_JSON" "$SDC"

python3 - "$RUN_DIR/configuration.json" <<'PY'
import json, os, sys
from pathlib import Path
period=float(os.environ['CLK_PERIOD'])
repair=os.environ['ENABLE_REPAIR_TIMING']=='1'
data={
 'design_id':os.environ['DESIGN_ID'],'run_id':os.environ['RUN_TAG'],'top':'fir_filter',
 'taps':int(os.environ['TAPS']),'data_width':int(os.environ['DATA_WIDTH']),
 'coeff_width':int(os.environ['COEFF_WIDTH']),'acc_width':int(os.environ['ACC_WIDTH']),
 'utilization':int(os.environ['UTIL']),'place_density':float(os.environ['PLACE_DENSITY']),
 'clock_period':period,'clock_period_ns':period,'frequency_mhz':1000.0/period,
 'enable_repair_timing':repair,
 'repair_timing_mode':'enabled' if repair else 'disabled_diagnostic'}
Path(sys.argv[1]).write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
PY

python3 - <<'PY'
import os,re
from pathlib import Path
source=Path('scripts/synth_fir.ys').read_text(encoding='utf-8')
replacements={
 '__DATA_WIDTH__':os.environ['DATA_WIDTH'], '__COEFF_WIDTH__':os.environ['COEFF_WIDTH'],
 '__TAPS__':os.environ['TAPS'], '__ACC_WIDTH__':os.environ['ACC_WIDTH'],
 '__LIB_FILE__':os.environ['LIBERTY'], '__NETLIST_JSON__':os.environ['NETLIST_JSON'],
 '__NETLIST_VERILOG__':os.environ['NETLIST']}
for token,value in replacements.items(): source=source.replace(token,value)
remaining=sorted(set(re.findall(r'__[A-Z0-9_]+__',source)))
if remaining: raise SystemExit('Unresolved Yosys placeholders: '+', '.join(remaining))
Path('fir_project/synth/synth_fir.generated.ys').write_text(source,encoding='utf-8')
PY

yosys -l "$RUN_DIR/synthesis.log" -s fir_project/synth/synth_fir.generated.ys
for file in "$NETLIST" "$NETLIST_JSON"; do [[ -s "$file" ]] || { echo "ERROR: Synthesis output missing: $file"; exit 3; }; done
python3 - "$NETLIST_JSON" <<'PY'
import json,sys
from pathlib import Path
module=json.loads(Path(sys.argv[1]).read_text()).get('modules',{}).get('fir_filter')
if not module or not module.get('cells') or not module.get('ports'):
 raise SystemExit('Synthesized JSON lacks fir_filter cells or ports.')
print(f"Yosys JSON valid: {len(module['cells'])} cells; {len(module['ports'])} ports")
PY

sed "s|__CLK_PERIOD__|${CLK_PERIOD}|g" constraints/fir_filter.sdc.in > "$SDC"
grep -q '__CLK_PERIOD__' "$SDC" && { echo "ERROR: Unresolved SDC clock placeholder."; exit 3; }
grep -E 'create_clock.*-period' "$SDC" || true
export NETLIST NETLIST_JSON SDC RUN_DIR UTIL PLACE_DENSITY ENABLE_REPAIR_TIMING

OPENROAD_STATUS=0
if openroad -exit scripts/flow_fir.tcl 2>&1 | tee "$RUN_DIR/openroad_log.txt"; then
  echo "OpenROAD process returned success."
else
  OPENROAD_STATUS=$?
  echo "WARNING: OpenROAD returned exit code $OPENROAD_STATUS; attempting partial extraction."
fi

FINAL_DEF_PRESENT=0; FINAL_ODB_PRESENT=0; PRE_DRT_DEF_PRESENT=0; PRE_DRT_ODB_PRESENT=0
[[ -s "$RUN_DIR/final.def" ]] && FINAL_DEF_PRESENT=1
[[ -s "$RUN_DIR/final.odb" ]] && FINAL_ODB_PRESENT=1
[[ -s "$RUN_DIR/pre_drt.def" ]] && PRE_DRT_DEF_PRESENT=1
[[ -s "$RUN_DIR/pre_drt.odb" ]] && PRE_DRT_ODB_PRESENT=1
printf 'final.def=%s final.odb=%s pre_drt.def=%s pre_drt.odb=%s\n' "$FINAL_DEF_PRESENT" "$FINAL_ODB_PRESENT" "$PRE_DRT_DEF_PRESENT" "$PRE_DRT_ODB_PRESENT"

EXTRACTION_STATUS=0
if python3 scripts/extract_fir_features.py \
 --netlist-json "$NETLIST_JSON" --run-dir "$RUN_DIR" --outdir "$RUN_DIR" \
 --top fir_filter --design-id "$DESIGN_ID" --run-id "$RUN_TAG" \
 --taps "$TAPS" --data-width "$DATA_WIDTH" --coeff-width "$COEFF_WIDTH" \
 --acc-width "$ACC_WIDTH" --utilization "$UTIL" --place-density "$PLACE_DENSITY" \
 --clock-period "$CLK_PERIOD"; then
 echo "Feature extraction completed."
else
 EXTRACTION_STATUS=$?
 echo "ERROR: Feature extraction failed with exit code $EXTRACTION_STATUS."
fi

required_outputs=(configuration.json preplacement_features.csv preplacement_features.json physical_targets.csv physical_targets.json features.csv features.json fir_features.csv extraction_status.json run_report.md)
MISSING_OUTPUTS=0
for name in "${required_outputs[@]}"; do
 if [[ -s "$RUN_DIR/$name" ]]; then echo "FOUND: $RUN_DIR/$name"; else echo "MISSING/EMPTY: $RUN_DIR/$name"; MISSING_OUTPUTS=$((MISSING_OUTPUTS+1)); fi
done
(( MISSING_OUTPUTS == 0 )) || { echo "ERROR: $MISSING_OUTPUTS extraction output(s) missing."; exit 5; }
(( EXTRACTION_STATUS == 0 )) || exit 5
(( OPENROAD_STATUS == 0 )) || { echo "ERROR: OpenROAD failed ($OPENROAD_STATUS); partial outputs preserved."; exit 4; }
(( FINAL_DEF_PRESENT == 1 && FINAL_ODB_PRESENT == 1 )) || { echo "ERROR: OpenROAD succeeded but final DEF/ODB are incomplete."; exit 4; }
echo "FIR flow completed successfully: $RUN_TAG"
