"""Targeted round-trip tests for the line table encoder/decoder.

The stdlib round-trip covers these shapes too, but only incidentally and only
for whatever the corpus happens to contain; these pin them down explicitly.
"""

import sys
import types

import pytest
from test_stdlib_roundtrip import line_ranges

from spasm import _core

Bytecode = _core.Bytecode
Instr = _core.Instr

PY = sys.version_info[:2]


def check(co):
    """Round-trip a code object and assert its line table survives."""
    new_co = Bytecode.from_code(co).to_code()
    assert line_ranges(new_co) == line_ranges(co)
    return new_co


def compile_func(source, name):
    """Compile `source` and return the code object of the function `name`."""
    top = compile(source, "<test>", "exec")
    for const in top.co_consts:
        if isinstance(const, types.CodeType) and const.co_name == name:
            return const
    msg = f"no function {name} in source"
    raise AssertionError(msg)


def from_scratch(linenos, firstlineno):
    """A code object returning None, one NOP per entry in `linenos`."""
    bc = Bytecode()
    bc.firstlineno = firstlineno
    instrs = [Instr("RESUME", 0, lineno=firstlineno)] if PY >= (3, 11) else []
    instrs += [Instr("NOP", lineno=lineno) for lineno in linenos]
    instrs += [
        Instr("LOAD_CONST", None, lineno=linenos[-1]),
        Instr("RETURN_VALUE", lineno=linenos[-1]),
    ]
    bc.instrs = instrs
    return bc.to_code()


def test_everything_on_firstlineno():
    # The table has to describe the code from offset 0 onwards, so a body that
    # never leaves the first line still needs an entry rather than none at all.
    co = compile_func("def f(): return 1", "f")
    assert {lineno for _, _, lineno in line_ranges(co)} == {1}
    check(co)


def test_leading_run_on_firstlineno():
    # Same, but with a line change after it: the entry for the first line is
    # the one that is easy to drop, which then slides every later entry down.
    co = compile_func("def f():\n    x = 1\n    return x\n", "f")
    check(co)


def test_no_line_range():
    # Compiler-generated instructions carry no line number at all. Which
    # constructs produce them varies by version, hence the skip rather than an
    # assertion — a generator prologue covers every version but 3.11.
    co = compile_func("def g(n):\n    for i in n:\n        yield i\n", "g")
    if not any(lineno is None for _, _, lineno in co.co_lines()):
        pytest.skip("no no-line range in the compiled output at this version")
    check(co)


@pytest.mark.parametrize("delta", [-129, -128, -127, 127, 128, 129, 400, -400])
def test_large_line_delta(delta):
    # 3.10 encodes deltas in a signed byte and reserves -128 as the "no line"
    # marker, so a real delta of exactly -128 has to be split rather than
    # emitted as it stands.
    firstlineno = 500
    co = from_scratch([firstlineno, firstlineno + delta, firstlineno], firstlineno)
    assert {lineno for _, _, lineno in line_ranges(co)} == {firstlineno, firstlineno + delta}
    check(co)


def test_extended_arg_before_line_change():
    # Offsets in the table are byte offsets, and an EXTENDED_ARG word is not a
    # logical instruction of its own, so the two only line up if the decoder
    # accounts for the folding.
    body = "\n".join(f"        x = {i}" for i in range(300))
    co = compile_func(f"def f(c):\n    if c:\n{body}\n    return c\n", "f")

    import dis

    assert any(i.opname == "EXTENDED_ARG" for i in dis.get_instructions(co))
    check(co)


def test_line_table_survives_repeated_round_trips():
    # Encoding is not idempotent by construction: a table that decodes right
    # but re-encodes differently only shows up on the second pass.
    co = compile_func(
        "def f(a):\n"
        "    try:\n"
        "        with a:\n"
        "            for i in a:\n"
        "                yield i\n"
        "    finally:\n"
        "        a.close()\n",
        "f",
    )
    once = check(co)
    check(once)
