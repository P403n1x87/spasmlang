import operator
import sys

import pytest

from spasm import Assembly
from spasm._core import Bytecode
from spasm._core import Instr
from spasm.bytecode import CO_GENERATOR
from spasm.bytecode import CO_NEWLOCALS
from spasm.bytecode import CO_NOFREE
from spasm.bytecode import CO_OPTIMIZED
from spasm.bytecode import BinaryOp
from spasm.bytecode import Compare
from spasm.bytecode import infer_flags

PY = sys.version_info[:2]
RESUME = "resume 0" if PY >= (3, 11) else ""

COMPARISONS = [
    (Compare.LT, operator.lt),
    (Compare.LE, operator.le),
    (Compare.EQ, operator.eq),
    (Compare.NE, operator.ne),
    (Compare.GT, operator.gt),
    (Compare.GE, operator.ge),
]


@pytest.mark.parametrize("compare,op", COMPARISONS)
@pytest.mark.parametrize("a,b", [(1, 2), (2, 2), (3, 2)])
def test_compare_oparg(compare, op, a, b):
    """Every comparison encodes correctly, including under specialisation.

    From 3.12 the oparg carries a mask telling the specialising interpreter
    which result types the comparison can produce, and from 3.13 the comparison
    index moved again. Getting the mask wrong only shows up once the adaptive
    interpreter has specialised the instruction, so the code object is run
    enough times to get there rather than just once.
    """
    asm = Assembly()
    asm.parse(
        rf"""
            {RESUME}
            load_const      {{a}}
            load_const      {{b}}
            compare_op      {{compare}}
            return_value
        """
    )

    code = asm.compile({"a": a, "b": b, "compare": compare})
    expected = op(a, b)
    assert {eval(code) for _ in range(100)} == {expected}  # noqa: S307


@pytest.mark.skipif(PY < (3, 11), reason="BINARY_OP was introduced in CPython 3.11")
@pytest.mark.parametrize(
    "binop,op",
    [
        (BinaryOp.ADD, operator.add),
        (BinaryOp.SUBTRACT, operator.sub),
        (BinaryOp.MULTIPLY, operator.mul),
        (BinaryOp.FLOOR_DIVIDE, operator.floordiv),
        (BinaryOp.REMAINDER, operator.mod),
    ],
)
def test_binary_oparg(binop, op):
    asm = Assembly()
    asm.parse(
        rf"""
            {RESUME}
            load_const      17
            load_const      5
            binary_op       {{binop}}
            return_value
        """
    )

    code = asm.compile({"binop": binop})
    assert {eval(code) for _ in range(100)} == {op(17, 5)}  # noqa: S307


def _empty(instrs=()):
    bc = Bytecode()
    bc.instrs = list(instrs)
    return bc


# CPython stopped setting CO_NOFREE in 3.11, so neither do we.
NOFREE = CO_NOFREE if PY < (3, 11) else 0


def test_infer_flags_module_vs_function():
    """A module body executes against a real dict; a function gets fast locals."""
    assert infer_flags(_empty(), is_function=False) == NOFREE
    assert infer_flags(_empty(), is_function=True) == CO_OPTIMIZED | CO_NEWLOCALS | NOFREE


@pytest.mark.skipif(PY >= (3, 11), reason="CPython stopped setting CO_NOFREE in 3.11")
def test_infer_flags_nofree_cleared_by_freevars():
    bc = _empty()
    bc.freevars = ["x"]
    assert not infer_flags(bc, is_function=True) & CO_NOFREE

    bc = _empty()
    bc.cellvars = ["x"]
    assert not infer_flags(bc, is_function=True) & CO_NOFREE


def test_infer_flags_generator():
    """A yield anywhere in the stream makes the code object a generator."""
    yielding = _empty([Instr("LOAD_CONST", 1), Instr("YIELD_VALUE", 0)])
    assert infer_flags(yielding, is_function=True) & CO_GENERATOR
    assert not infer_flags(_empty([Instr("LOAD_CONST", 1)]), is_function=True) & CO_GENERATOR


# Module-level on purpose: a nested def would also carry CO_NESTED. And no
# docstring, or 3.14 would add CO_HAS_DOCSTRING, which spasm cannot set since
# it has no notion of a docstring.
def _reference(who):
    return who


def test_assembled_function_flags_match_cpython():
    """A sub-code assembled by spasm carries the flags CPython would give it."""
    asm = Assembly()
    asm.parse(
        rf"""
        code greet(who)
            {RESUME}
            load_fast   $who
            return_value
        end

            {RESUME}
            load_const  .greet
            {"" if PY >= (3, 11) else 'load_const "greet"'}
            make_function 0
            store_name  $greet
            load_const  None
            return_value
        """
    )

    _globals = {}
    exec(asm.compile(), _globals)  # noqa: S102

    assert _globals["greet"].__code__.co_flags == _reference.__code__.co_flags
