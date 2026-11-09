`timescale 1ns/1ps
module tb_fir_filter #(
    parameter integer DATA_WIDTH  = 8,
    parameter integer COEFF_WIDTH = 8,
    parameter integer TAPS        = 8,
    parameter integer ACC_WIDTH   = DATA_WIDTH + COEFF_WIDTH + 8
);
    reg clk = 1'b0;
    reg rst = 1'b1;
    reg valid_in = 1'b0;
    reg signed [DATA_WIDTH-1:0] sample_in = 0;
    wire valid_out;
    wire signed [ACC_WIDTH-1:0] sample_out;

    fir_filter #(
        .DATA_WIDTH(DATA_WIDTH),
        .COEFF_WIDTH(COEFF_WIDTH),
        .TAPS(TAPS),
        .ACC_WIDTH(ACC_WIDTH)
    ) dut (
        .clk(clk), .rst(rst), .valid_in(valid_in), .sample_in(sample_in),
        .valid_out(valid_out), .sample_out(sample_out)
    );

    always #5 clk = ~clk;

    function integer coeff;
        input integer index;
        begin
            case (index)
                0: coeff = 3;
                1: coeff = -2;
                2: coeff = 5;
                3: coeff = 1;
                4: coeff = -4;
                5: coeff = 6;
                6: coeff = 2;
                7: coeff = -1;
                8: coeff = 1;
                9: coeff = -3;
                10: coeff = 4;
                11: coeff = -2;
                default: coeff = 0;
            endcase
        end
    endfunction

    task automatic check_impulse;
        input integer impulse_value;
        integer cycle;
        integer expected;
        begin
            // First cycle registers the impulse; later cycles expose each tap.
            for (cycle = 0; cycle <= TAPS; cycle = cycle + 1) begin
                @(negedge clk);
                valid_in = 1'b1;
                sample_in = (cycle == 0) ? impulse_value : 0;
                @(posedge clk);
                #1;
                expected = (cycle == 0) ? 0 : impulse_value * coeff(cycle - 1);
                if (valid_out !== 1'b1)
                    $fatal(1, "FAIL: valid_out was low at impulse cycle %0d", cycle);
                if (sample_out !== expected)
                    $fatal(1, "FAIL: TAPS=%0d DATA_WIDTH=%0d cycle=%0d expected=%0d got=%0d",
                           TAPS, DATA_WIDTH, cycle, expected, sample_out);
            end

            @(negedge clk);
            valid_in = 1'b0;
            sample_in = 0;
            @(posedge clk);
            #1;
            if (valid_out !== 1'b0)
                $fatal(1, "FAIL: valid_out did not deassert during input bubble.");
            if (sample_out !== expected)
                $fatal(1, "FAIL: sample_out should hold its previous value during input bubble.");
        end
    endtask

    initial begin
        repeat (3) @(posedge clk);
        @(negedge clk);
        rst = 1'b0;

        check_impulse(10);

        // Reset between tests, then verify signed negative multiplication.
        @(negedge clk);
        rst = 1'b1;
        valid_in = 1'b0;
        sample_in = 0;
        repeat (2) @(posedge clk);
        @(negedge clk);
        rst = 1'b0;

        check_impulse(-9);

        $display("PASS: FIR impulse, signed arithmetic, valid and reset checks completed.");
        $finish;
    end
endmodule
