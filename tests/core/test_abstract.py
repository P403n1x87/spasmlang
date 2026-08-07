"""Tests for abstract const/local/name manipulation."""

import dis
import sys
import types

from spasm import _core

Bytecode = _core.Bytecode
Instr = _core.Instr
Label = _core.Label

LOAD_CONST = dis.opmap["LOAD_CONST"]
LOAD_FAST = dis.opmap["LOAD_FAST"]
STORE_FAST = dis.opmap["STORE_FAST"]
LOAD_GLOBAL = dis.opmap["LOAD_GLOBAL"]
RETURN_VALUE = dis.opmap.get("RETURN_VALUE") or dis.opmap.get("RETURN_CONST")


# ── bc.consts / bc.varnames / bc.names ────────────────────────────────────────


def test_consts_exposed():
    def f():
        return 42

    bc = Bytecode.from_code(f.__code__)
    assert isinstance(bc.consts, list)
    assert 42 in bc.consts


def test_varnames_exposed():
    def f(x, y):
        return x + y

    bc = Bytecode.from_code(f.__code__)
    assert "x" in bc.varnames
    assert "y" in bc.varnames


def test_names_exposed():
    def f():
        print()

    bc = Bytecode.from_code(f.__code__)
    assert isinstance(bc.names, list)
    assert "print" in bc.names


# ── add_const ─────────────────────────────────────────────────────────────────


def test_add_const_existing():
    def f():
        return 42

    bc = Bytecode.from_code(f.__code__)
    idx = bc.add_const(42)
    assert isinstance(idx, int)
    assert bc.consts[idx] == 42


def test_add_const_new():
    def f():
        return 1

    bc = Bytecode.from_code(f.__code__)
    assert 99 not in bc.consts
    idx = bc.add_const(99)
    assert bc.consts[idx] == 99


def test_add_name():
    def f():
        return 1

    bc = Bytecode.from_code(f.__code__)
    idx = bc.add_name("my_func")
    assert bc.names[idx] == "my_func"


def test_add_varname():
    def f(x):
        return x

    bc = Bytecode.from_code(f.__code__)
    idx = bc.add_varname("x")
    assert bc.varnames[idx] == "x"  # already there


# ── LOAD_CONST with new value ─────────────────────────────────────────────────


def test_insert_load_const_new_value():
    """Replace a const/immediate arg of 1 with 100 and verify it executes."""
    # In 3.14+ small integers use LOAD_SMALL_INT (arg = value directly).
    # In older versions they use LOAD_CONST (arg = co_consts index → decoded value).
    LOAD_SMALL_INT = dis.opmap.get("LOAD_SMALL_INT")
    CONST_OPS = {op for name, op in dis.opmap.items() if op in set(dis.hasconst)}

    def f(x):
        return x + 1

    bc = Bytecode.from_code(f.__code__)

    replaced = False
    for instr in bc.instrs:
        # LOAD_SMALL_INT: arg is the integer value directly
        if LOAD_SMALL_INT and instr.op == LOAD_SMALL_INT and instr.arg == 1:
            instr.arg = 100
            replaced = True
            break
        # LOAD_CONST / RETURN_CONST: arg is abstract Python value after from_code
        if instr.op in CONST_OPS and instr.arg == 1:
            instr.arg = 100
            replaced = True
            break

    assert replaced, "could not find instruction loading constant 1"
    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    assert new_fn(0) == 100  # 0 + 100


def test_load_const_round_trip_abstract():
    """Abstract args survive from_code → to_code intact."""

    # Use a constant > 255 to guarantee LOAD_CONST (not LOAD_SMALL_INT in 3.14+)
    def f(x):
        return x + 256

    bc = Bytecode.from_code(f.__code__)
    const_instrs = [i for i in bc.instrs if i.op == LOAD_CONST]
    assert any(i.arg == 256 for i in const_instrs), (
        f"Expected abstract LOAD_CONST(256), got: {[i.arg for i in const_instrs]}"
    )

    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    assert new_fn(0) == 256


def test_load_fast_abstract():
    """LOAD_FAST arg is the varname string, not an index."""

    def f(x, y):
        return x + y

    bc = Bytecode.from_code(f.__code__)
    fast_instrs = [i for i in bc.instrs if i.op == LOAD_FAST]
    for i in fast_instrs:
        assert isinstance(i.arg, str), f"LOAD_FAST arg should be str, got {i.arg!r}"


def test_store_fast_abstract():
    """STORE_FAST arg is the varname string after from_code."""

    def f(x):
        y = x + 1
        return y

    bc = Bytecode.from_code(f.__code__)
    store_instrs = [i for i in bc.instrs if i.op == STORE_FAST]
    for i in store_instrs:
        assert isinstance(i.arg, str), f"STORE_FAST arg should be str, got {i.arg!r}"


def test_add_new_local_and_store():
    """Add a new local variable and store a constant into it."""
    STORE_FAST_OP = dis.opmap["STORE_FAST"]

    def f(x):
        return x

    bc = Bytecode.from_code(f.__code__)

    # Add a new local 'tmp' and constant 100. Both are interned on demand by
    # the Instr arguments below; these calls are what is under test.
    bc.add_varname("tmp")
    bc.add_const(100)

    # Insert LOAD_CONST(100) + STORE_FAST('tmp') at position 0
    bc.instrs.insert(0, Instr(STORE_FAST_OP, "tmp"))
    bc.instrs.insert(0, Instr(LOAD_CONST, 100))

    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    # Function still returns x, but now also stores 100 into tmp (dead store)
    assert new_fn(42) == 42


if __name__ == "__main__":
    test_consts_exposed()
    test_varnames_exposed()
    test_names_exposed()
    test_add_const_existing()
    test_add_const_new()
    test_add_name()
    test_add_varname()
    test_insert_load_const_new_value()
    test_load_const_round_trip_abstract()
    test_load_fast_abstract()
    test_store_fast_abstract()
    test_add_new_local_and_store()
    print(f"All abstract API tests passed (Python {sys.version})")
