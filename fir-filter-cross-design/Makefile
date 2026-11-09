IVERILOG ?= iverilog
VVP      ?= vvp

BUILD_DIR := build
SIM_FILE  := $(BUILD_DIR)/tb_fir_filter.vvp

RTL_FILE  := rtl/fir_filter.v
TB_FILE   := tb/tb_fir_filter.v

.PHONY: all sim clean

all: sim

sim: $(SIM_FILE)
	$(VVP) $(SIM_FILE)

$(SIM_FILE): $(RTL_FILE) $(TB_FILE)
	mkdir -p $(BUILD_DIR)
	$(IVERILOG) -g2012 -Wall -s tb_fir_filter -o $(SIM_FILE) $(RTL_FILE) $(TB_FILE)

clean:
	rm -rf $(BUILD_DIR)
