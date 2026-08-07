"""Tests for building a Bytecode/code object from scratch (no from_code())."""

import dis
import gc
import sys
import types

from spasm import _core

Bytecode = _core.Bytecode
Instr = _core.Instr

# BINARY_OP (3.11+) replaced the separate BINARY_ADD/BINARY_SUBTRACT/...
# opcodes used on 3.10; NB_ADD is index 0 into dis._nb_ops either way.
ADD_OP = "BINARY_OP" if sys.version_info >= (3, 11) else "BINARY_ADD"


def _add_instrs():
    instrs = []
    if sys.version_info >= (3, 11):
        instrs.append(Instr("RESUME", 0))
    instrs += [
        Instr("LOAD_FAST", "x"),
        Instr("LOAD_FAST", "y"),
        Instr(ADD_OP, 0),
        Instr("RETURN_VALUE"),
    ]
    return instrs


def test_bytecode_has_no_direct_positional_construction_surprises():
    # Bytecode() takes no required args and returns a usable empty template.
    bc = Bytecode()
    assert bc.instrs == []
    assert bc.exc_entries == []
    assert bc.consts == []
    assert bc.names == []
    assert bc.varnames == []
    assert bc.freevars == []
    assert bc.cellvars == []
    assert bc.argcount == 0
    assert bc.flags == 0
    assert bc.firstlineno == 1
    assert bc.filename == "<string>"
    assert bc.name == "<bytecode>"
    assert bc.qualname == "<bytecode>"


def test_bytecode_constructor_seeds_instrs():
    instrs = _add_instrs()
    bc = Bytecode(instrs)
    assert bc.instrs == instrs


def test_bytecode_constructor_rejects_non_instr_items():
    try:
        Bytecode([1, 2, 3])
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError")


def test_build_add_function_from_scratch():
    # No stacksize assignment anywhere — co_stacksize is always computed
    # automatically by to_code().
    bc = Bytecode(_add_instrs())
    bc.argcount = 2
    bc.varnames = ["x", "y"]
    bc.name = "add"
    bc.qualname = "add"

    new_code = bc.to_code()
    assert isinstance(new_code, types.CodeType)
    assert new_code.co_stacksize == 2  # both LOAD_FASTs on the stack at once

    add = types.FunctionType(new_code, {}, "add")
    assert add(3, 4) == 7
    assert add(-1, 1) == 0


def test_filename_name_qualname_settable():
    bc = Bytecode()
    bc.filename = "myfile.py"
    bc.name = "myfunc"
    bc.qualname = "MyClass.myfunc"
    assert bc.filename == "myfile.py"
    assert bc.name == "myfunc"
    assert bc.qualname == "MyClass.myfunc"


def test_filename_name_qualname_type_checked():
    bc = Bytecode()
    for attr in ("filename", "name", "qualname"):
        try:
            setattr(bc, attr, 123)
        except TypeError:
            pass
        else:
            raise AssertionError(f"expected TypeError setting {attr}")


def test_filename_name_qualname_survive_source_code_object_collection():
    # Regression test: from_code() must own its filename/name/qualname
    # references rather than borrowing them from the source code object,
    # which may be garbage collected before the Bytecode is done with them.
    def make_bc():
        def f(x):
            return x + 1

        return Bytecode.from_code(f.__code__)

    bc = make_bc()
    gc.collect()

    assert bc.filename == __file__
    assert bc.name == "f"
    new_code = bc.to_code()
    assert isinstance(new_code, types.CodeType)


def test_instr_accepts_opname_string():
    i = Instr("LOAD_FAST", "x")
    assert i.op == dis.opmap["LOAD_FAST"]


def test_instr_op_setter_accepts_opname_string():
    i = Instr("NOP")
    i.op = "LOAD_FAST"
    assert i.op == dis.opmap["LOAD_FAST"]


def test_instr_rejects_unknown_opname():
    try:
        Instr("NOT_A_REAL_OPCODE")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_instr_still_accepts_int_opcode():
    op = dis.opmap["LOAD_FAST"]
    i = Instr(op, "x")
    assert i.op == op
