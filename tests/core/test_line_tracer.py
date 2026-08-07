"""Instrumentation test: inject a call to a Python function before every
source line of a function, using nothing but the public Bytecode/Instr API.

This exercises .instrs mutation end-to-end (as opposed to test_from_scratch's
build-from-nothing case): decode an existing function, walk its instructions,
splice in new ones at each line boundary, and verify the hook fires with the
expected line numbers. Line numbers are checked directly against hand-derived
expectations (relative to each function's own co_firstlineno) rather than
against sys.settrace() — mixing sys.settrace into a pytest-run test is fragile
(it can fight with pytest's own trace/coverage machinery), and our injected
hook's firing rule (once per *static* line-boundary instruction) is simpler
than — and intentionally does not attempt to replicate — CPython's own
line-event semantics, which additionally re-fires on backward-jump loop
resumption.
"""

import dis
import sys
import types

from spasm import _core

Bytecode = _core.Bytecode
Instr = _core.Instr

RESUME_OP = dis.opmap.get("RESUME")  # None on 3.10 — no RESUME there
END_FOR_OP = dis.opmap.get("END_FOR")  # None before 3.12

# Opcodes that must stay immediately adjacent to what precedes them and are
# never real line-trace injection points: RESUME must be the true first
# instruction (3.11+); END_FOR must immediately follow FOR_ITER's
# exhaustion-jump target (3.12+) — inserting between them corrupts the
# interpreter's loop-iterator cleanup. Neither corresponds to a real
# sys.settrace 'line' event anyway.
_NO_INJECT_BEFORE = {op for op in (RESUME_OP, END_FOR_OP) if op is not None}


def instrument_line_tracer(bc, tracer):
    """Insert a call to tracer(lineno) before the first instruction of every
    source line, in place. `tracer` must accept a single int argument and
    is referenced directly as a LOAD_CONST value (no globals/names needed).

    Jump targets are already Labels (Bytecode.from_code() resolves them
    during decode) and stay attached to their target Instr regardless of
    how many instructions we splice in around it — no separate
    symbolification step needed before mutating .instrs.
    """
    new_instrs = []
    last_lineno = None

    for instr in bc.instrs:
        if instr.op in _NO_INJECT_BEFORE:
            new_instrs.append(instr)
            last_lineno = instr.lineno
            continue

        if instr.lineno >= 0 and instr.lineno != last_lineno:
            ln = instr.lineno
            hook_start = len(new_instrs)
            if sys.version_info >= (3, 11):
                # CALL's stack protocol is [NULL, callable, args...] on
                # 3.11/3.12, but flipped to [callable, NULL, args...] on
                # 3.13+.
                if sys.version_info >= (3, 13):
                    new_instrs.append(Instr("LOAD_CONST", tracer, lineno=ln))
                    new_instrs.append(Instr("PUSH_NULL", lineno=ln))
                else:
                    new_instrs.append(Instr("PUSH_NULL", lineno=ln))
                    new_instrs.append(Instr("LOAD_CONST", tracer, lineno=ln))
                new_instrs.append(Instr("LOAD_CONST", ln, lineno=ln))
                if sys.version_info < (3, 12):
                    # PRECALL is a 3.11-only specialization checkpoint
                    # between pushing args and CALL; removed in 3.12.
                    new_instrs.append(Instr("PRECALL", 1, lineno=ln))
                new_instrs.append(Instr("CALL", 1, lineno=ln))
                new_instrs.append(Instr("POP_TOP", lineno=ln))
            else:
                new_instrs.append(Instr("LOAD_CONST", tracer, lineno=ln))
                new_instrs.append(Instr("LOAD_CONST", ln, lineno=ln))
                new_instrs.append(Instr("CALL_FUNCTION", 1, lineno=ln))
                new_instrs.append(Instr("POP_TOP", lineno=ln))
            last_lineno = ln

            # If a jump already targets `instr` (it's the first instruction
            # of this line only in *static* order — control can also arrive
            # here directly via a jump), move its labels onto our hook's
            # first instruction. Otherwise a jump would land straight on
            # `instr`, skipping the hook we just inserted before it.
            if instr.labels:
                new_instrs[hook_start].labels = list(instr.labels)
                instr.labels = []

        new_instrs.append(instr)

    bc.instrs = new_instrs
    # No manual stacksize bookkeeping needed — to_code() computes it fresh.


def _run_instrumented(fn, *args, **kwargs):
    """Instrument fn, call it, and return (result, hits, base) where hits is
    the list of line numbers the injected hook was called with (in order)
    and base = fn.__code__.co_firstlineno, so callers can express expected
    lines as offsets from the def line rather than hardcoding absolute
    numbers that would break if this file is edited."""
    hits = []

    def tracer(lineno):
        hits.append(lineno)

    bc = Bytecode.from_code(fn.__code__)
    instrument_line_tracer(bc, tracer)
    new_code = bc.to_code()
    assert new_code is not None

    instrumented = types.FunctionType(new_code, fn.__globals__, fn.__name__)
    result = instrumented(*args, **kwargs)
    return result, hits, fn.__code__.co_firstlineno


def test_straight_line_function():
    def f(a, b):
        x = a + b
        y = x * 2
        return y

    result, hits, base = _run_instrumented(f, 3, 4)
    assert result == 14
    assert hits == [base + 1, base + 2, base + 3]


def test_function_with_branch():
    def classify(n):
        if n < 0:
            return "negative"
        elif n == 0:
            return "zero"
        else:
            return "positive"

    result, hits, base = _run_instrumented(classify, -5)
    assert result == "negative"
    assert hits == [base + 1, base + 2]

    result, hits, base = _run_instrumented(classify, 0)
    assert result == "zero"
    assert hits == [base + 1, base + 3, base + 4]

    result, hits, base = _run_instrumented(classify, 5)
    assert result == "positive"
    # bare "else:" generates no code of its own (no condition to test), so
    # it never gets its own hit — control falls straight through to the
    # return statement's line.
    assert hits == [base + 1, base + 3, base + 6]


def test_function_with_loop_visits_lines_repeatedly():
    def total(n):
        s = 0
        for i in range(n):
            s += i
        return s

    result, hits, base = _run_instrumented(total, 5)
    assert result == 10
    # setup line once, loop body once per iteration, return once
    assert hits[0] == base + 1
    assert hits.count(base + 3) == 5
    assert hits[-1] == base + 4
    # the "for" line itself is only hit on the initial pass — our injection
    # fires once per *static* line-boundary instruction, not once per
    # backward-jump re-entry (see module docstring).
    assert hits.count(base + 2) == 1


def test_function_with_try_except():
    def safe_div(a, b):
        try:
            return a // b
        except ZeroDivisionError:
            return None

    result, hits, base = _run_instrumented(safe_div, 10, 2)
    assert result == 5
    assert hits == [base + 1, base + 2]

    result, hits, base = _run_instrumented(safe_div, 10, 0)
    assert result is None
    assert hits == [base + 1, base + 2, base + 3, base + 4]
