
# FIR physical-design flow for OpenROAD / Sky130HD.
#
# Supports:
#   FLOORPLAN_MODE=utilization
#   FLOORPLAN_MODE=fixed
#
# Fixed mode uses explicit die and core coordinates in microns.
#
# Dataset policy:
# Synthesized-netlist features are circuit predictors.
# Placement/routing measurements are physical targets.
# Floorplan conditions are recorded separately.

# ------------------------------------------------------------
# 1. Validate environment variables
# ------------------------------------------------------------

foreach var {
    TECH_LEF CELL_LEF LIBERTY NETLIST SDC RUN_DIR
    UTIL PLACE_DENSITY FLOORPLAN_MODE
} {
    if {![info exists ::env($var)] || $::env($var) eq ""} {
        puts stderr "ERROR: required environment variable $var is not set"
        exit 1
    }
}

set floorplan_mode $::env(FLOORPLAN_MODE)

if {$floorplan_mode ni {utilization fixed}} {
    puts stderr "ERROR: FLOORPLAN_MODE must be utilization or fixed"
    exit 1
}

foreach var {UTIL PLACE_DENSITY} {
    if {![string is double -strict $::env($var)]} {
        puts stderr "ERROR: $var must be numeric"
        exit 1
    }
}

if {$::env(UTIL) <= 0 || $::env(UTIL) > 80} {
    puts stderr "ERROR: UTIL must be > 0 and <= 80 percent"
    exit 1
}

if {$::env(PLACE_DENSITY) <= 0 || $::env(PLACE_DENSITY) > 1} {
    puts stderr "ERROR: PLACE_DENSITY must be > 0 and <= 1"
    exit 1
}

if {$floorplan_mode eq "utilization"} {

    foreach var {FLOORPLAN_UTIL CORE_AREA_SCALE} {
        if {![info exists ::env($var)] ||
            $::env($var) eq "" ||
            ![string is double -strict $::env($var)]} {
            puts stderr "ERROR: $var must be numeric in utilization mode"
            exit 1
        }
    }

    if {$::env(FLOORPLAN_UTIL) <= 0 || $::env(FLOORPLAN_UTIL) > 80} {
        puts stderr "ERROR: FLOORPLAN_UTIL must be > 0 and <= 80 percent"
        exit 1
    }

    if {$::env(CORE_AREA_SCALE) < 0.60 ||
        $::env(CORE_AREA_SCALE) > 1.00} {
        puts stderr "ERROR: CORE_AREA_SCALE must be between 0.60 and 1.00"
        exit 1
    }

} else {

    foreach var {
        DIE_WIDTH DIE_HEIGHT
        CORE_LL_X CORE_LL_Y
        CORE_UR_X CORE_UR_Y
    } {
        if {![info exists ::env($var)] ||
            $::env($var) eq "" ||
            ![string is double -strict $::env($var)] ||
            $::env($var) <= 0} {
            puts stderr "ERROR: $var must be a positive number in fixed mode"
            exit 1
        }
    }

    if {$::env(CORE_LL_X) >= $::env(CORE_UR_X) ||
        $::env(CORE_LL_Y) >= $::env(CORE_UR_Y) ||
        $::env(CORE_LL_X) < 0 ||
        $::env(CORE_LL_Y) < 0 ||
        $::env(CORE_UR_X) > $::env(DIE_WIDTH) ||
        $::env(CORE_UR_Y) > $::env(DIE_HEIGHT)} {
        puts stderr "ERROR: Core coordinates must define a positive area inside the die"
        exit 1
    }
}

set enable_repair_timing 1

if {[info exists ::env(ENABLE_REPAIR_TIMING)]} {
    set enable_repair_timing $::env(ENABLE_REPAIR_TIMING)
}

if {$enable_repair_timing ni {0 1}} {
    puts stderr "ERROR: ENABLE_REPAIR_TIMING must be 0 or 1"
    exit 1
}

# ------------------------------------------------------------
# 2. Initialize run information
# ------------------------------------------------------------

file mkdir $::env(RUN_DIR)

if {[info exists ::env(RUN_TAG)] && $::env(RUN_TAG) ne ""} {
    set ::fir_run_id $::env(RUN_TAG)
} else {
    set ::fir_run_id [file tail $::env(RUN_DIR)]
}

set ::fir_last_completed_stage "initialization"
set ::fir_status_csv "$::env(RUN_DIR)/fir_features_status.csv"
set ::fir_diagnostics "$::env(RUN_DIR)/flow_diagnostics.log"

# ------------------------------------------------------------
# 3. Logging and status helper procedures
# ------------------------------------------------------------

proc fir_csv_escape {value} {
    set value [string map [list "\r" " " "\n" " "] $value]
    set value [string map [list "\"" "\"\""] $value]
    return "\"$value\""
}

proc fir_status {status stage} {
    set fd [open $::fir_status_csv a]
    puts $fd "[fir_csv_escape $::fir_run_id],[fir_csv_escape $status],[fir_csv_escape $stage],[clock seconds]"
    close $fd
}

proc fir_log {message} {
    puts $message
    set fd [open $::fir_diagnostics a]
    puts $fd $message
    close $fd
}

proc fir_step {stage command_list} {
    set ::fir_current_stage $stage
    fir_log "STAGE: $stage"
    fir_status "IN_PROGRESS" $::fir_last_completed_stage

    if {[catch {uplevel #0 $command_list} result options]} {
        fir_log "ERROR in stage '$stage': $result"
        fir_status "FAILED" $::fir_last_completed_stage
        return -options $options $result
    }

    set ::fir_last_completed_stage $stage
    fir_status "IN_PROGRESS" $::fir_last_completed_stage
    fir_log "STAGE_COMPLETED: $stage"
    return $result
}

# ------------------------------------------------------------
# 4. Reset run-specific status and diagnostic files
# ------------------------------------------------------------

set fd [open $::fir_status_csv w]
puts $fd "\"run_id\",\"status\",\"last_completed_stage\",\"unix_timestamp\""
close $fd

set fd [open $::fir_diagnostics w]
puts $fd "FIR physical-design diagnostics for $::fir_run_id"
close $fd

fir_status "STARTED" "initialization"

fir_log "RUN_ID: $::fir_run_id"
fir_log "FLOORPLAN_MODE: $floorplan_mode"
fir_log "ENABLE_REPAIR_TIMING: $enable_repair_timing"
fir_log "REQUESTED_UTILIZATION: $::env(UTIL)"
fir_log "PLACEMENT_DENSITY: $::env(PLACE_DENSITY)"

if {$floorplan_mode eq "utilization"} {
    fir_log "CORE_AREA_SCALE: $::env(CORE_AREA_SCALE)"
    fir_log "EFFECTIVE_FLOORPLAN_UTILIZATION: $::env(FLOORPLAN_UTIL)"
} else {
    fir_log "DIE_WIDTH_UM: $::env(DIE_WIDTH)"
    fir_log "DIE_HEIGHT_UM: $::env(DIE_HEIGHT)"
    fir_log "CORE_LL_X_UM: $::env(CORE_LL_X)"
    fir_log "CORE_LL_Y_UM: $::env(CORE_LL_Y)"
    fir_log "CORE_UR_X_UM: $::env(CORE_UR_X)"
    fir_log "CORE_UR_Y_UM: $::env(CORE_UR_Y)"

    if {[info exists ::env(CORE_AREA_UM2)]} {
        fir_log "CORE_AREA_UM2: $::env(CORE_AREA_UM2)"
    }
}

fir_log "ML_INPUT_POLICY: synthesized-netlist features are circuit predictors; physical metrics are targets"

# ------------------------------------------------------------
# 5. Read technology and design
# ------------------------------------------------------------

fir_step "read_technology_lef" [list read_lef $::env(TECH_LEF)]
fir_step "read_cell_lef" [list read_lef $::env(CELL_LEF)]
fir_step "read_liberty" [list read_liberty $::env(LIBERTY)]
fir_step "read_netlist" [list read_verilog $::env(NETLIST)]
fir_step "link_design" [list link_design fir_filter]
fir_step "read_sdc" [list read_sdc $::env(SDC)]

# ------------------------------------------------------------
# 6. Floorplan initialization
# ------------------------------------------------------------

if {$floorplan_mode eq "fixed"} {

    # Explicit die and core rectangles.
    # Coordinates are in microns.

    set die_area [list \
        0 0 \
        $::env(DIE_WIDTH) \
        $::env(DIE_HEIGHT)]

    set core_area [list \
        $::env(CORE_LL_X) \
        $::env(CORE_LL_Y) \
        $::env(CORE_UR_X) \
        $::env(CORE_UR_Y)]

    fir_log "INITIALIZING_FIXED_FLOORPLAN"
    fir_log "DIE_AREA_UM: $die_area"
    fir_log "CORE_AREA_UM: $core_area"

    fir_step "initialize_floorplan" [list initialize_floorplan \
        -die_area $die_area \
        -core_area $core_area \
        -site unithd]

} else {

    # Existing utilization-based floorplan.

    fir_log "INITIALIZING_UTILIZATION_FLOORPLAN"
    fir_log "FLOORPLAN_UTIL: $::env(FLOORPLAN_UTIL)"

    fir_step "initialize_floorplan" [list initialize_floorplan \
        -utilization $::env(FLOORPLAN_UTIL) \
        -aspect_ratio 1.0 \
        -core_space 5.0 \
        -site unithd]
}

fir_step "make_tracks" [list make_tracks]
fir_step "set_wire_rc" [list set_wire_rc -layer met2]

# ------------------------------------------------------------
# 7. Top-level I/O pin placement
# ------------------------------------------------------------

fir_step "place_io_pins" [list place_pins \
    -hor_layers met3 \
    -ver_layers met2]

# ------------------------------------------------------------
# 8. Global placement
# ------------------------------------------------------------

fir_step "global_placement" [list global_placement \
    -density $::env(PLACE_DENSITY)]

# ------------------------------------------------------------
# 9. Design repair
# ------------------------------------------------------------

if {[catch {repair_design} result]} {
    fir_log "WARNING: repair_design was skipped/failed: $result"
} else {
    fir_log "OPTIONAL_STEP_COMPLETED: repair_design"
}

# ------------------------------------------------------------
# 10. Detailed placement
# ------------------------------------------------------------

fir_step "detailed_placement" [list detailed_placement]

# ------------------------------------------------------------
# 11. Optional timing repair
# ------------------------------------------------------------

if {$enable_repair_timing == 1} {
    fir_log "REPAIR_TIMING_ENABLED: true"

    if {[catch {repair_timing -setup -setup_margin 0.2} result]} {
        fir_log "WARNING: repair_timing was skipped/failed: $result"
    } else {
        fir_log "OPTIONAL_STEP_COMPLETED: repair_timing"
    }

} else {
    fir_log "REPAIR_TIMING_ENABLED: false"
    fir_log "OPTIONAL_STEP_SKIPPED: repair_timing"
}

# ------------------------------------------------------------
# 12. Initial and final global-routing congestion measurements
# ------------------------------------------------------------

set pre_grt_odb "$::env(RUN_DIR)/pre_grt.odb"

set initial_congestion_gcell_report \
    "$::env(RUN_DIR)/initial_congestion.rpt"

set initial_congestion_summary \
    "$::env(RUN_DIR)/initial_congestion_summary.rpt"

set initial_congestion_log \
    "$::env(RUN_DIR)/initial_congestion_openroad.log"

set initial_congestion_script \
    "$::env(RUN_DIR)/initial_congestion_diagnostic.tcl"

set final_congestion_gcell_report \
    "$::env(RUN_DIR)/congestion.rpt"

set final_congestion_summary \
    "$::env(RUN_DIR)/final_congestion_summary.rpt"

set congestion_status_report \
    "$::env(RUN_DIR)/congestion_status.rpt"

fir_step "write_pre_grt_odb" [list write_db $pre_grt_odb]

# Run initial diagnostic routing in a fresh OpenROAD process.

set diagnostic_fd [open $initial_congestion_script w]

puts $diagnostic_fd [list read_db $pre_grt_odb]
puts $diagnostic_fd [list read_liberty $::env(LIBERTY)]
puts $diagnostic_fd [list read_sdc $::env(SDC)]
puts $diagnostic_fd [list set_wire_rc -layer met2]

puts $diagnostic_fd [list global_route \
    -congestion_iterations 0 \
    -allow_congestion \
    -congestion_report_file $initial_congestion_gcell_report \
    -verbose]

puts $diagnostic_fd {if {[catch {has_congestion} initial_flag]} {
    puts "INITIAL_GLOBAL_ROUTE_HAS_CONGESTION: unavailable"
} else {
    set initial_status [expr {$initial_flag ? "true" : "false"}]
    puts "INITIAL_GLOBAL_ROUTE_HAS_CONGESTION: $initial_status"
}}

puts $diagnostic_fd [list tee -file $initial_congestion_summary \
    [list report_congestion]]

close $diagnostic_fd

set initial_congestion_status "unavailable"

fir_log "INITIAL_GLOBAL_ROUTE_DIAGNOSTIC_STARTED_IN_FRESH_PROCESS"

set diagnostic_command \
    [list [info nameofexecutable] -exit $initial_congestion_script]

if {[catch {
    exec {*}$diagnostic_command > $initial_congestion_log 2>@1
} diagnostic_result]} {

    fir_log "WARNING: Initial congestion diagnostic process failed: $diagnostic_result"
    fir_log "INITIAL_CONGESTION_DIAGNOSTIC_LOG: $initial_congestion_log"

} else {

    fir_log "INITIAL_GLOBAL_ROUTE_DIAGNOSTIC_COMPLETED"
    fir_log "INITIAL_CONGESTION_DIAGNOSTIC_LOG: $initial_congestion_log"

    if {[file exists $initial_congestion_log]} {
        set diagnostic_log_fd [open $initial_congestion_log r]
        set diagnostic_text [read $diagnostic_log_fd]
        close $diagnostic_log_fd

        if {[regexp -nocase \
            {INITIAL_GLOBAL_ROUTE_HAS_CONGESTION:\s*(true|false)} \
            $diagnostic_text -> initial_flag_text]} {

            set initial_congestion_status \
                [string tolower $initial_flag_text]
        }
    }
}

fir_log "INITIAL_GLOBAL_ROUTE_HAS_CONGESTION: $initial_congestion_status"

if {[file exists $initial_congestion_summary]} {
    fir_log "INITIAL_CONGESTION_SUMMARY: $initial_congestion_summary"
} else {
    fir_log "WARNING: Initial numeric congestion summary was not generated"
}

# Main production global routing.

fir_log "FINAL_GLOBAL_ROUTE_STARTED"

fir_step "global_routing" [list global_route \
    -congestion_iterations 50 \
    -allow_congestion \
    -congestion_report_file $final_congestion_gcell_report \
    -verbose]

set final_congestion_status "unavailable"

if {[catch {has_congestion} final_flag]} {

    fir_log "FINAL_GLOBAL_ROUTE_HAS_CONGESTION: unavailable"
    fir_log "WARNING: Final has_congestion query failed: $final_flag"

} else {

    set final_congestion_status \
        [expr {$final_flag ? "true" : "false"}]

    fir_log "FINAL_GLOBAL_ROUTE_HAS_CONGESTION: $final_congestion_status"
}

if {[catch {
    tee -file $final_congestion_summary { report_congestion }
} result]} {

    fir_log "WARNING: Final numeric congestion summary unavailable: $result"

} else {

    fir_log "FINAL_CONGESTION_SUMMARY: $final_congestion_summary"
}

set fd [open $congestion_status_report w]

puts $fd "INITIAL_GLOBAL_ROUTE_HAS_CONGESTION: $initial_congestion_status"
puts $fd "GLOBAL_ROUTE_HAS_CONGESTION: $final_congestion_status"
puts $fd "FINAL_GLOBAL_ROUTE_HAS_CONGESTION: $final_congestion_status"
puts $fd "INITIAL_CONGESTION_SUMMARY: $initial_congestion_summary"
puts $fd "FINAL_CONGESTION_SUMMARY: $final_congestion_summary"
puts $fd "INITIAL_CONGESTION_DIAGNOSTIC_LOG: $initial_congestion_log"

close $fd

# Global-route wirelength report.

if {[catch {
    report_wire_length -net * -global_route \
        -file "$::env(RUN_DIR)/wirelength_global.rpt"
} result]} {

    fir_log "WARNING: global-route wirelength report unavailable: $result"

} else {

    fir_log "GLOBAL_ROUTE_WIRELENGTH_REPORT: $::env(RUN_DIR)/wirelength_global.rpt"
}

# ------------------------------------------------------------
# 13. Global-route parasitic estimation and timing reports
# ------------------------------------------------------------

fir_step "estimate_global_route_parasitics" \
    [list estimate_parasitics -global_routing]

catch {
    report_checks -path_delay max -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup_global_route.rpt"
}

catch {
    report_worst_slack -max \
        > "$::env(RUN_DIR)/wns_setup_global_route.rpt"
}

catch {
    report_tns \
        > "$::env(RUN_DIR)/tns_setup_global_route.rpt"
}

# ------------------------------------------------------------
# 14. Save pre-detailed-routing checkpoints
# ------------------------------------------------------------

fir_step "write_pre_drt_odb" \
    [list write_db "$::env(RUN_DIR)/pre_drt.odb"]

fir_step "write_pre_drt_def" \
    [list write_def "$::env(RUN_DIR)/pre_drt.def"]

fir_log "PRE_DRT_CHECKPOINTS_SAVED"

# ------------------------------------------------------------
# 15. Detailed routing
# ------------------------------------------------------------

set detailed_route_drc_report \
    "$::env(RUN_DIR)/detailed_route_drc.rpt"

fir_step "detailed_routing" [list detailed_route \
    -output_drc $detailed_route_drc_report]

puts "SUCCESS: Detailed routing"
fir_log "SUCCESS: Detailed routing"

# ------------------------------------------------------------
# 16. Save final routed artifacts
# ------------------------------------------------------------

fir_step "write_final_def" \
    [list write_def "$::env(RUN_DIR)/final.def"]

fir_step "write_final_odb" \
    [list write_db "$::env(RUN_DIR)/final.odb"]

if {[catch {
    report_wire_length -net * -detailed_route \
        -file "$::env(RUN_DIR)/wirelength_detailed.rpt"
} result]} {

    fir_log "WARNING: detailed-route wirelength report unavailable: $result"

} else {

    fir_log "DETAILED_ROUTE_WIRELENGTH_REPORT: $::env(RUN_DIR)/wirelength_detailed.rpt"
}

# ------------------------------------------------------------
# 17. Final timing and area reports
# ------------------------------------------------------------

if {[catch {
    report_checks -path_delay max -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_setup.rpt"
} result]} {

    fir_log "WARNING: setup timing report unavailable: $result"
}

if {[catch {
    report_checks -path_delay min -format full_clock_expanded \
        > "$::env(RUN_DIR)/timing_hold.rpt"
} result]} {

    fir_log "WARNING: hold timing report unavailable: $result"
}

if {[catch {
    report_worst_slack -max \
        > "$::env(RUN_DIR)/wns_setup.rpt"
} result]} {

    fir_log "WARNING: setup WNS report unavailable: $result"
}

if {[catch {
    report_worst_slack -min \
        > "$::env(RUN_DIR)/wns_hold.rpt"
} result]} {

    fir_log "WARNING: hold WNS report unavailable: $result"
}

if {[catch {
    report_tns > "$::env(RUN_DIR)/tns_setup.rpt"
} result]} {

    fir_log "WARNING: TNS report unavailable: $result"
}

if {[catch {
    report_design_area > "$::env(RUN_DIR)/design_area.rpt"
} result]} {

    fir_log "WARNING: design-area report unavailable: $result"
}

# ------------------------------------------------------------
# 18. Validate final routed artifacts
# ------------------------------------------------------------

if {![file exists "$::env(RUN_DIR)/final.def"] ||
    [file size "$::env(RUN_DIR)/final.def"] == 0} {

    fir_log "ERROR: final.def is missing or empty"
    fir_status "FAILED" $::fir_last_completed_stage
    error "final.def is missing or empty"
}

if {![file exists "$::env(RUN_DIR)/final.odb"] ||
    [file size "$::env(RUN_DIR)/final.odb"] == 0} {

    fir_log "ERROR: final.odb is missing or empty"
    fir_status "FAILED" $::fir_last_completed_stage
    error "final.odb is missing or empty"
}

# ------------------------------------------------------------
# 19. Mark flow as completed
# ------------------------------------------------------------

set ::fir_last_completed_stage "flow_completed"

fir_status "COMPLETED" $::fir_last_completed_stage
fir_log "FLOW_COMPLETED: $::env(RUN_DIR)"

puts "FIR physical-design flow completed. Results: $::env(RUN_DIR)"
