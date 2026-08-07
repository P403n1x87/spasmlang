"""Low-level bytecode API.

:mod:`spasm._core` is a C++ extension that carries opcode tables baked in for
the interpreter it was compiled against. It deliberately keeps its surface
narrow and mechanical; this module is the thin Python layer on top of it that
holds the parts which are version-dependent bookkeeping rather than data
structure:

* :class:`Compare` and :class:`BinaryOp`, the symbolic opargs whose encoding
  moved between versions,
* :func:`encode_name_arg`, which packs a ``co_names`` index together with the
  flag bit that 3.11+ ``LOAD_GLOBAL`` and 3.12+ ``LOAD_ATTR`` carry alongside
  it,
* :func:`infer_flags`, which derives ``co_flags`` for a code object being
  built from scratch,
* :data:`UNSET`, the "this instruction takes no argument" sentinel.

Keeping these here rather than in :mod:`spasm.asm` means anything building on
the core gets them, and the assembler stays about assembly.
"""

import dis
import enum
import opcode as _opcode_module
import sys
import typing as t

from spasm._core import Bytecode
from spasm._core import ExcEntry
from spasm._core import Instr
from spasm._core import Label

__all__ = [
    "UNSET",
    "BinaryOp",
    "Bytecode",
    "Compare",
    "ExcEntry",
    "Instr",
    "Label",
    "compare_oparg",
    "encode_name_arg",
    "infer_flags",
]

PY311 = sys.version_info >= (3, 11)
PY312 = sys.version_info >= (3, 12)
PY313 = sys.version_info >= (3, 13)


class _Unset:
    """Sentinel for "this instruction takes no argument"."""

    _instance: t.ClassVar["_Unset | None"] = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


# ---------------------------------------------------------------------------
# Symbolic opargs
# ---------------------------------------------------------------------------


class Compare(enum.IntEnum):
    """Argument for the ``COMPARE_OP`` opcode.

    The members are the logical comparison indices. The oparg CPython actually
    wants is version-dependent — 3.12 moved the comparison into the high bits
    and put a type mask in the low ones, and 3.13 shifted it again and added a
    cast-to-bool bit — so always go through :func:`compare_oparg`.
    """

    LT = 0
    LE = 1
    EQ = 2
    NE = 3
    GT = 4
    GE = 5

    if PY313:
        # Same comparisons, but with the "cast the result to bool" bit set.
        LT_CAST = 0 + 16
        LE_CAST = 1 + 16
        EQ_CAST = 2 + 16
        NE_CAST = 3 + 16
        GT_CAST = 4 + 16
        GE_CAST = 5 + 16


# See compare_masks in CPython's compile.c: the low bits tell the specialising
# interpreter which result types the comparison can produce.
_COMPARE_MASKS = {
    Compare.LT: 2,
    Compare.LE: 2 + 8,
    Compare.EQ: 8,
    Compare.NE: 1 + 2 + 4,
    Compare.GT: 4,
    Compare.GE: 4 + 8,
}


def compare_oparg(value: Compare) -> int:
    """Encode a :class:`Compare` as the ``COMPARE_OP`` oparg for this version."""
    if PY313:
        mask = _COMPARE_MASKS[Compare(value & 0b1111)]
        return mask + ((value & 0b1111) << 5) + (value & 16)
    if PY312:
        mask = _COMPARE_MASKS[Compare(value & 0b1111)]
        return mask + (value << 4)
    return int(value)


class BinaryOp(enum.IntEnum):
    """Argument for the ``BINARY_OP`` opcode (3.11+), used as the oparg as-is."""

    ADD = 0
    AND = 1
    FLOOR_DIVIDE = 2
    LSHIFT = 3
    MATRIX_MULTIPLY = 4
    MULTIPLY = 5
    REMAINDER = 6
    OR = 7
    POWER = 8
    RSHIFT = 9
    SUBTRACT = 10
    TRUE_DIVIDE = 11
    XOR = 12
    INPLACE_ADD = 13
    INPLACE_AND = 14
    INPLACE_FLOOR_DIVIDE = 15
    INPLACE_LSHIFT = 16
    INPLACE_MATRIX_MULTIPLY = 17
    INPLACE_MULTIPLY = 18
    INPLACE_REMAINDER = 19
    INPLACE_OR = 20
    INPLACE_POWER = 21
    INPLACE_RSHIFT = 22
    INPLACE_SUBTRACT = 23
    INPLACE_TRUE_DIVIDE = 24
    INPLACE_XOR = 25


# ---------------------------------------------------------------------------
# Name arguments
# ---------------------------------------------------------------------------

# The core abstracts CONST/LOCAL/FREE arguments but not NAME ones, because
# LOAD_GLOBAL (3.11+) and LOAD_ATTR (3.12+) pack a flag bit alongside the
# co_names index and so cannot be represented by the name alone.
_HASNAME = frozenset(dis.hasname)


def is_name_op(op: int) -> bool:
    """Whether ``op`` takes a ``co_names`` index as its argument."""
    return op in _HASNAME


# Opcodes the interpreter writes into a code object at runtime but that are
# never part of a code object as assembled: the INSTRUMENTED_* family (which
# sys.monitoring patches in), the JIT's ENTER_EXECUTOR, and the pseudo-opcodes
# the compiler uses internally and resolves before emitting. They have no
# meaningful encoding here, and handing some of them to the interpreter in a
# freshly built code object crashes it outright, so refuse them up front.
_MIN_PSEUDO = getattr(_opcode_module, "MIN_PSEUDO_OPCODE", 1 << 30)
_MIN_INSTRUMENTED = getattr(_opcode_module, "MIN_INSTRUMENTED_OPCODE", 1 << 30)
_INTERNAL_OPNAMES = frozenset(
    name
    for name, op in dis.opmap.items()
    if op >= _MIN_PSEUDO or op >= _MIN_INSTRUMENTED or name in {"ENTER_EXECUTOR", "RESERVED", "CACHE"}
)


def is_internal_op(opname: str) -> bool:
    """Whether ``opname`` is an opcode that cannot appear in assembled code."""
    return opname in _INTERNAL_OPNAMES


def encode_name_arg(code: Bytecode, opname: str, name: str, *, flag: bool = False) -> int:
    """Encode a name argument as the oparg for ``opname``.

    ``name`` is interned into ``code``'s name table. ``flag`` is the extra bit
    some opcodes pack into the low position: for ``LOAD_GLOBAL`` on 3.11+ it
    means "push a NULL alongside the global" (for a subsequent call), and for
    ``LOAD_ATTR`` on 3.12+ it means "this is a method lookup", i.e. what
    ``LOAD_METHOD`` used to do.
    """
    index = code.add_name(name)
    if (opname == "LOAD_GLOBAL" and PY311) or (opname == "LOAD_ATTR" and PY312):
        return (index << 1) | int(bool(flag))
    return index


# ---------------------------------------------------------------------------
# Code flags
# ---------------------------------------------------------------------------

CO_OPTIMIZED = 0x0001
CO_NEWLOCALS = 0x0002
CO_VARARGS = 0x0004
CO_VARKEYWORDS = 0x0008
CO_NESTED = 0x0010
CO_GENERATOR = 0x0020
CO_NOFREE = 0x0040
CO_COROUTINE = 0x0080
CO_ITERABLE_COROUTINE = 0x0100
CO_ASYNC_GENERATOR = 0x0200

_YIELD_OPS = frozenset(
    dis.opmap[name] for name in ("YIELD_VALUE", "YIELD_FROM", "RETURN_GENERATOR", "GEN_START") if name in dis.opmap
)


def infer_flags(code: Bytecode, *, is_function: bool = False, is_async: bool = False) -> int:
    """Derive ``co_flags`` for a code object being assembled from scratch.

    Two flags are deliberately *not* inferred, because nothing in the code
    object records them and guessing would produce something CPython would not:

    * ``CO_VARARGS`` / ``CO_VARKEYWORDS`` — the instruction stream doesn't
      distinguish ``*args`` from an ordinary parameter;
    * ``CO_NESTED`` — that's a property of lexical scope, not of the code;
    * ``CO_HAS_DOCSTRING`` (3.14+) — nothing here models a docstring.

    A caller that needs those must add them itself.
    """
    flags = 0

    # A function body gets fast locals and a fresh namespace; module and class
    # bodies execute against a real dict and get neither.
    if is_function:
        flags |= CO_OPTIMIZED | CO_NEWLOCALS

    # CPython stopped setting CO_NOFREE in 3.11 — it computes it nowhere and
    # every code object comes back without it, so setting it would make our
    # code objects differ from equivalent compiled ones.
    if not PY311 and not code.freevars and not code.cellvars:
        flags |= CO_NOFREE

    if any(instr.op in _YIELD_OPS for instr in code.instrs):
        if is_async:
            flags |= CO_ASYNC_GENERATOR
        else:
            flags |= CO_GENERATOR
    elif is_async:
        flags |= CO_COROUTINE

    return flags
