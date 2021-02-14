# Copyright (c) 2020 Antmicro <www.antmicro.com>
from abc import abstractproperty

from ..hdl import *
from ..lib.cdc import ResetSynchronizer
from ..build import *


__all__ = ["Xilinx7SeriesSymbiflowPlatform"]


class Xilinx7SeriesSymbiflowPlatform(TemplatedPlatform):
    """
    Required tools:
        * ``synth``
        * ``pack``
        * ``place``
        * ``route``
        * ``write_fasm``
        * ``write_bitstream``

    The environment is populated by running the script specified in the environment variable
    ``NMIGEN_ENV_Symbiflow``, if present.

    Available overrides:
        * ``add_constraints``: inserts commands in XDC file.
    """

    toolchain = "Symbiflow"

    device  = abstractproperty()
    package = abstractproperty()
    speed   = abstractproperty()
    grade   = None

    required_tools = [
        "synth",
        "pack",
        "place",
        "route",
        "write_fasm",
        "write_bitstream"
    ]
    file_templates = {
        **TemplatedPlatform.build_script_templates,
        "{{name}}.v": r"""
            /* {{autogenerated}} */
            {{emit_verilog()}}
        """,
        "{{name}}.debug.v": r"""
            /* {{autogenerated}} */
            {{emit_debug_verilog()}}
        """,
        "{{name}}.pcf": r"""
            # {{autogenerated}}
            {% for port_name, pin_name, attrs in platform.iter_port_constraints_bits() -%}
                set_io {{port_name}} {{pin_name}}
            {% endfor %}
        """,
        "{{name}}.xdc": r"""
            # {{autogenerated}}
            {% for port_name, pin_name, attrs in platform.iter_port_constraints_bits() -%}
                set_property LOC {{pin_name}} [get_ports {{ '{' + port_name + '}' }} ]
                {% for attr_name, attr_value in attrs.items() -%}
                    set_property {{attr_name}} {{attr_value}} [get_ports {{ '{' + port_name + '}' }} }]
                {% endfor %}
            {% endfor %}
            {{get_override("add_constraints")|default("# (add_constraints placeholder)")}}
        """,
        "{{name}}.sdc": r"""
            # {{autogenerated}}
            {% for net_signal, port_signal, frequency in platform.iter_clock_constraints() -%}
                {% if port_signal is none -%}
                    create_clock -period {{1000000000/frequency}} {{net_signal.name}}
                {% endif %}
            {% endfor %}
        """
    }
    command_templates = [
        r"""
        {{invoke_tool("synth")}}
            -t {{name}}
            -v {% for file in platform.iter_extra_files(".v", ".sv", ".vhd", ".vhdl") -%} {{file}} {% endfor %} {{name}}.v
            -p {{platform.device}}{{platform.package}}-{{platform.speed}}
            -x {{name}}.xdc
        """,
        r"""
        {{invoke_tool("pack")}}
            -e {{name}}.eblif
            -P {{platform.device}}{{platform.package}}-{{platform.speed}}
            -s {{name}}.sdc
        """,
        r"""
        {{invoke_tool("place")}}
            -e {{name}}.eblif
            -p {{name}}.pcf
            -n {{name}}.net
            -P {{platform.device}}{{platform.package}}-{{platform.speed}}
            -s {{name}}.sdc
        """,
        r"""
        {{invoke_tool("route")}}
            -e {{name}}.eblif
            -P {{platform.device}}{{platform.package}}-{{platform.speed}}
            -s {{name}}.sdc
        """,
        r"""
        {{invoke_tool("write_fasm")}}
            -e {{name}}.eblif
            -P {{platform.device}}{{platform.package}}-{{platform.speed}}
        """,
        r"""
        {{invoke_tool("write_bitstream")}}
            -f {{name}}.fasm
            -p {{platform.device}}{{platform.package}}-{{platform.speed}}
            -b {{name}}.bit
        """
    ]

    def create_missing_domain(self, name):
        if name == "sync" and self.default_clk is not None:
            clk_i = self.request(self.default_clk).i
            if self.default_rst is not None:
                rst_i = self.request(self.default_rst).i

            m = Module()
            cd_sync = ClockDomain("sync", reset_less=self.default_rst is None)
            m.domains += cd_sync
            m.submodules += Instance("BUFG", i_I=clk_i, o_O=cd_sync.clk)
            self.add_clock_constraint(cd_sync.clk, self.default_clk_frequency)
            if self.default_rst is not None:
                m.submodules.reset_sync = ResetSynchronizer(rst_i, domain="sync")
            return m

    def _get_xdr_buffer(self, m, pin, *, i_invert=False, o_invert=False):
        def get_ineg(y, invert):
            if invert:
                a = Signal.like(y, name_suffix="_n")
                m.d.comb += y.eq(~a)
                return a
            else:
                return y

        def get_oneg(a, invert):
            if invert:
                y = Signal.like(a, name_suffix="_n")
                m.d.comb += y.eq(~a)
                return y
            else:
                return a

        if "i" in pin.dir:
            if pin.xdr < 2:
                pin_i  = get_ineg(pin.i,  i_invert)
            elif pin.xdr == 2:
                pin_i0 = get_ineg(pin.i0, i_invert)
                pin_i1 = get_ineg(pin.i1, i_invert)
        if "o" in pin.dir:
            if pin.xdr < 2:
                pin_o  = get_oneg(pin.o,  o_invert)
            elif pin.xdr == 2:
                pin_o0 = get_oneg(pin.o0, o_invert)
                pin_o1 = get_oneg(pin.o1, o_invert)

        i = o = t = None
        if "i" in pin.dir:
            i = Signal(pin.width, name="{}_xdr_i".format(pin.name))
        if "o" in pin.dir:
            o = Signal(pin.width, name="{}_xdr_o".format(pin.name))
        if pin.dir in ("oe", "io"):
            t = Signal(1,         name="{}_xdr_t".format(pin.name))

        if pin.xdr == 0:
            if "i" in pin.dir:
                i = pin_i
            if "o" in pin.dir:
                o = pin_o
            if pin.dir in ("oe", "io"):
                t = ~pin.oe
        else:
            assert False

        return (i, o, t)

    def get_input(self, pin, port, attrs, invert):
        self._check_feature("single-ended input", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, i_invert=invert)
        for bit in range(len(port)):
            m.submodules["{}_{}".format(pin.name, bit)] = Instance("IBUF",
                i_I=port[bit],
                o_O=i[bit]
            )
        return m

    def get_output(self, pin, port, attrs, invert):
        self._check_feature("single-ended output", pin, attrs,
                            valid_xdrs=(0, 1, 2), valid_attrs=True)
        m = Module()
        i, o, t = self._get_xdr_buffer(m, pin, o_invert=invert)
        m.d.comb += port.eq(self._invert_if(invert, o))
        return m
