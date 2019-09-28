from abc import abstractproperty

from ..hdl import *
from ..build import *


__all__ = ["AlteraPlatform"]


class AlteraPlatform(TemplatedPlatform):
    """
    Required tools:
        * ``quartus_map``
        * ``quartus_fit``
        * ``quartus_asm``
        * ``quartus_sta``

    The environment is populated by running the script specified in the environment variable
    ``NMIGEN_Quartus_env``, if present.

    Available overrides:
        * ``nproc``: sets the number of cores used by all tools.
        * ``quartus_map_opts``: adds extra options for ``quartus_map``.
        * ``quartus_fit_opts``: adds extra options for ``quartus_fit``.
        * ``quartus_asm_opts``: adds extra options for ``quartus_asm``.
        * ``quartus_sta_opts``: adds extra options for ``quartus_sta``.

    Build products:
        * ``*.rpt``: toolchain reports.
        * ``{{name}}.rbf``: raw binary bitstream.
    """

    toolchain = "Quartus"

    device  = abstractproperty()
    package = abstractproperty()
    speed   = abstractproperty()
    suffix  = ""

    required_tools = [
        "quartus_map",
        "quartus_fit",
        "quartus_asm",
        "quartus_sta",
    ]

    file_templates = {
        **TemplatedPlatform.build_script_templates,
        "build_{{name}}.sh": r"""
            # {{autogenerated}}
            if [ -n "$NMIGEN_{{platform.toolchain}}_env" ]; then
                QUARTUS_ROOTDIR=$(dirname $(dirname "$NMIGEN_{{platform.toolchain}}_env"))
                # Quartus' qenv.sh does not work with `set -e`.
                . "$NMIGEN_{{platform.toolchain}}_env"
            fi
            set -e{{verbose("x")}}
            {{emit_commands("sh")}}
        """,
        "{{name}}.v": r"""
            /* {{autogenerated}} */
            {{emit_verilog()}}
        """,
        "{{name}}.debug.v": r"""
            /* {{autogenerated}} */
            {{emit_debug_verilog()}}
        """,
        "{{name}}.qsf": r"""
            # {{autogenerated}}
            {% if get_override("nproc") -%}
                set_global_assignment -name NUM_PARALLEL_PROCESSORS {{get_override("nproc")}}
            {% endif %}

            {% for file in platform.iter_extra_files(".v") -%}
                set_global_assignment -name VERILOG_FILE "{{file}}"
            {% endfor %}
            {% for file in platform.iter_extra_files(".sv") -%}
                set_global_assignment -name SYSTEMVERILOG_FILE "{{file}}"
            {% endfor %}
            set_global_assignment -name VERILOG_FILE {{name}}.v
            set_global_assignment -name TOP_LEVEL_ENTITY {{name}}

            set_global_assignment -name DEVICE {{platform.device}}{{platform.package}}{{platform.speed}}{{platform.suffix}}
            {% for port_name, pin_name, extras in platform.iter_port_constraints_bits() -%}
                set_location_assignment -to "{{port_name}}" PIN_{{pin_name}}
                {% for key, value in extras.items() -%}
                    set_instance_assignment -to "{{port_name}}" -name {{key}} "{{value}}"
                {% endfor %}
            {% endfor %}

            set_global_assignment -name GENERATE_RBF_FILE ON
        """,
        "{{name}}.sdc": r"""
            {% for signal, frequency in platform.iter_clock_constraints() -%}
                create_clock -period {{1000000000/frequency}} [get_nets {{signal|hierarchy("/")}}]
            {% endfor %}
        """,
    }
    command_templates = [
        r"""
        {{get_tool("quartus_map")}}
            {{get_override("quartus_map_opts")|options}}
            --rev={{name}} {{name}}
        """,
        r"""
        {{get_tool("quartus_fit")}}
            {{get_override("quartus_fit_opts")|options}}
            --rev={{name}} {{name}}
        """,
        r"""
        {{get_tool("quartus_asm")}}
            {{get_override("quartus_asm_opts")|options}}
            --rev={{name}} {{name}}
        """,
        r"""
        {{get_tool("quartus_sta")}}
            {{get_override("quartus_sta_opts")|options}}
            --rev={{name}} {{name}}
        """,
    ]

    def create_missing_domain(self, name):
        # TODO: investigate this
        return super().create_missing_domain(name)

    # TODO: fix all of the following
    @staticmethod
    def _invert_if(invert, value):
        if invert:
            return ~value
        else:
            return value

    def _add_ff(self, m, xdr, src, dest, clk, kind):
        if xdr == 0:
            m.d.comb += dest.eq(src)
            return

        m.submodules["{}_dff_{}_{}".format(pin.name, kind, bit)] += Instance("dff",
            i_d=src,
            i_clk=clk,
            i_clrn=1,
            i_prn=1,
            o_q=dest
        )
    
    # Despite the altiobuf manual saying ENABLE_BUS_HOLD is optional, Quartus requires it to be specified.

    def get_input(self, pin, port, attrs, invert):
        self._check_feature("single-ended input", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)

        m = Module()

        ff_i = Signal(pin.width)

        if pin.xdr == 1:
            pin.i.attrs["useioff"] = "1"

        for bit in range(pin.width):
            clk = pin.i_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, ff_i[bit]), pin.i[bit], clk, "i")

            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_in",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE",
                i_datain=port[bit],
                o_dataout=ff_i[bit]
            )

        return m

    def get_output(self, pin, port, attrs, invert):
        self._check_feature("single-ended output", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)

        m = Module()

        ff_o = Signal(pin.width)

        if pin.xdr == 1:
            pin.o.attrs["useioff"] = "1"

        for bit in range(pin.width):
            clk = pin.o_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, pin.o[bit]), ff_o[bit], clk, "o")

            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_out",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE", 
                i_datain=ff_o[bit],
                o_dataout=port[bit]
            )
            
        return m

    def get_tristate(self, pin, port, attrs, invert):
        self._check_feature("single-ended tristate", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)

        m = Module()

        ff_o = Signal(pin.width)
        ff_oe = Signal(pin.width)

        if pin.xdr == 1:
            pin.o.attrs["useioff"] = "1"
            pin.oe.attrs["useioff"] = "1"

        for bit in range(pin.width):
            clk = pin.o_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, pin.o[bit]), ff_o[bit], clk, "o")
            self._add_ff(m, pin.xdr, pin.oe[bit], ff_oe[bit], clk, "oe")

            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_out",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE", 
                p_USE_OE="TRUE",
                i_datain=ff_o[bit],
                i_oe=ff_oe[bit],
                o_dataout=port[bit]
            )

        return m

    def get_input_output(self, pin, port, attrs, invert):
        self._check_feature("single-ended input/output", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)

        m = Module()

        ff_i = Signal(pin.width)
        ff_o = Signal(pin.width)
        ff_oe = Signal(pin.width)

        if pin.xdr == 1:
            pin.i.attrs["useioff"] = "1"
            pin.o.attrs["useioff"] = "1"
            pin.oe.attrs["useioff"] = "1"

        for bit in range(pin.width):
            iclk = pin.i_clk if pin.xdr != 0 else None
            oclk = pin.o_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, ff_i[bit]), pin.i[bit], iclk, "i")
            self._add_ff(m, pin.xdr, self._invert_if(invert, pin.o[bit]), ff_o[bit], oclk, "o")
            self._add_ff(m, pin.xdr, pin.oe[bit], ff_oe[bit], oclk, "oe")

            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_bidir",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE", 
                i_datain=ff_o[bit],
                i_oe=ff_oe[bit],
                o_dataout=ff_i[bit],
                io_dataio=port[bit]
            )

        return m

    def get_diff_input(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential input", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)
        m = Module()

        ff_i = Signal(pin.width)

        if pin.xdr == 1:
            pin.i.attrs["useioff"] = "1"

        for bit in range(pin.width):
            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_in",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE", 
                p_USE_DIFFERENTIAL_MODE="TRUE",
                i_datain=p_port[bit],
                i_datain_b=n_port[bit],
                o_dataout=ff_i[bit]
            )
            
            clk = pin.i_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, ff_i[bit]), pin.i[bit], clk, "i")
 
        return m

    def get_diff_output(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential output", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)
        m = Module()

        ff_o = Signal(pin.width)

        if pin.xdr == 1:
            pin.o.attrs["useioff"] = "1"

        for bit in range(pin.width):
            clk = pin.o_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, pin.o[bit]), ff_o[bit], pin.o_clk, "o")

            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_out",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE", 
                p_USE_DIFFERENTIAL_MODE="TRUE",
                i_datain=ff_o[bit],
                o_dataout=p_port[bit],
                o_dataout_b=n_port[bit]
            )

        return m

    def get_diff_tristate(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential tristate", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)
        m = Module()

        ff_o = Signal(pin.width)
        ff_oe = Signal(pin.width)

        if pin.xdr == 1:
            pin.o.attrs["useioff"] = "1"
            pin.oe.attrs["useioff"] = "1"

        for bit in range(pin.width):
            clk = pin.o_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, pin.o[bit]), ff_o[bit], clk, "o")
            self._add_ff(m, pin.xdr, pin.oe[bit], ff_oe[bit], clk, "oe")

            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_out",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE", 
                p_USE_DIFFERENTIAL_MODE="TRUE",
                p_USE_OE="TRUE",
                i_datain=ff_o[bit],
                i_oe=ff_oe[bit],
                o_dataout=p_port[bit],
                o_dataout_b=n_port[bit]
            )

        return m

    def get_diff_input_output(self, pin, p_port, n_port, attrs, invert):
        self._check_feature("differential input/output", pin, attrs,
                            valid_xdrs=(0,1), valid_attrs=True)
        m = Module()

        ff_i = Signal(pin.width)
        ff_o = Signal(pin.width)
        ff_oe = Signal(pin.width)

        if pin.xdr == 1:
            pin.i.attrs["useioff"] = "1"
            pin.o.attrs["useioff"] = "1"
            pin.oe.attrs["useioff"] = "1"

        for bit in range(pin.width):
            iclk = pin.i_clk if pin.xdr != 0 else None
            oclk = pin.o_clk if pin.xdr != 0 else None

            self._add_ff(m, pin.xdr, self._invert_if(invert, ff_i[bit]), pin.i[bit], iclk, "i")
            self._add_ff(m, pin.xdr, self._invert_if(invert, pin.o[bit]), ff_o[bit], oclk, "o")
            self._add_ff(m, pin.xdr, pin.oe[bit], ff_oe[bit], oclk, "oe")

            m.submodules["{}_buf_{}".format(pin.name, bit)] = Instance("altiobuf_bidir",
                p_NUMBER_OF_CHANNELS=1,
                p_ENABLE_BUS_HOLD="FALSE", 
                p_USE_DIFFERENTIAL_MODE="TRUE",
                i_datain=ff_o[bit],
                i_oe=ff_oe[bit],
                o_dataout=ff_i[bit],
                io_dataio=p_port[bit],
                io_dataio_b=n_port[bit]
            )

        return m
