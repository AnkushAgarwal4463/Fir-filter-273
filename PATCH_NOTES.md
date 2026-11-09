# FIR cross-design physical-design patch

This patch aligns the FIR flow with the 273-run experiment:
- taps: 4, 8, 12
- data widths: 8, 16, 24, 32, 40, 48 (12 taps uses width 8 only)
- utilization: 20, 30, 40, 50, 60, 70, 80 percent
- clock periods: 5, 7.5, 10 ns
- coefficient width: 8
- accumulator width: data width + coefficient width + 8

## Files included

- `.github/workflows/fir-physical-design.yml`
- `fir-filter-cross-design/rtl/fir_filter.v`
- `fir-filter-cross-design/tb/tb_fir_filter.v`
- `fir-filter-cross-design/scripts/run_fir_flow.sh`
- `fir-filter-cross-design/scripts/flow_fir.tcl`
- `fir-filter-cross-design/scripts/synth_fir.ys`
- `fir-filter-cross-design/scripts/extract_fir_features.py`
- `fir-filter-cross-design/scripts/combine_fir_results.py`
- `fir-filter-cross-design/constraints/fir_filter.sdc.in`
- `fir-filter-cross-design/config/sky130_example.env`

The current `run_fir_flow.sh`, `flow_fir.tcl`, and workflow were retained from the latest files in the Library. The extractor and result combiner were updated; RTL and testbench were corrected/expanded. The Yosys, SDC, and environment files are included as the matching templates from the previously shared project files.

## ML data policy

Use `preplacement_features.csv` as the predictor matrix. It contains only numeric synthesized-netlist features plus requested utilization and clock-period constraints. It excludes design identity, netlist path, RTL parameters, categorical diagnostics, and post-placement/routing outcomes. Use `physical_targets.csv` for labels/targets. `features.csv` and `combined_fir_features.csv` are archival/diagnostic records, not direct model inputs.

## Validation performed

- `bash -n` passed for `run_fir_flow.sh`.
- Python byte-compilation passed for the extractor and combiner.
- The combiner generated exactly 273 rows in an empty-artifact sweep test and produced the expected first/last run IDs.
- The extractor passed a synthetic-netlist smoke test; its pre-placement CSV contained numeric predictors only and excluded identity/path and physical-target fields.
- The workflow YAML parsed successfully with PyYAML.

Yosys, Icarus Verilog, and OpenROAD are not installed in this execution environment, so no RTL simulation, synthesis, or physical-design run was performed here.
