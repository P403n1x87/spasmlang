"""Assemble every opcode the running interpreter has.

The rest of the assembly tests cover a handful of opcodes chosen for the
control flow they exercise. This one is about breadth instead: it says nothing
about whether a given instruction *does* the right thing, only that the
assembler knows the mnemonic, encodes the argument in the kind the opcode
expects, and produces a code object that disassembles back to what was written.

That is enough to catch a whole class of gap — an opcode missing from a
generated table, an argument kind mapped to the wrong table, a mnemonic the
parser cannot spell — across the entire instruction set rather than the part of
it the other tests happen to use.
"""

import dis
import opcode as _opcode
import sys

import pytest

from spasm import Assembly
from spasm.bytecode import is_internal_op

PY = sys.version_info[:2]

# Deep enough that no single instruction's pops take the stack negative, which
# the depth analysis rejects. The values are never used: the assembled code is
# disassembled, not run. The store declares a local, so that the opcodes taking
# a variable index have one to point at.
PROLOGUE = (["resume 0"] if PY >= (3, 11) else []) + ["load_const 1"] * 12 + ["store_fast $x"]

ASSEMBLABLE = sorted(name for name in dis.opmap if not is_internal_op(name) and name != "EXTENDED_ARG")
# LOAD_METHOD became a pseudo-opcode in 3.12 but is still accepted as a way of
# spelling LOAD_ATTR, so it is not among the names the assembler turns away.
INTERNAL = sorted(name for name in dis.opmap if is_internal_op(name) and name != "LOAD_METHOD")

# Superinstructions such as LOAD_FAST_LOAD_FAST pack two variable indices into
# one argument, so they take a plain integer rather than a variable name. They
# are spelled BASE1_BASE2 with both halves in haslocal, which is the same
# structural rule the build-time table generation uses.
_LOCAL_NAMES = {name for name, op in dis.opmap.items() if op in dis.haslocal}
PACKED_LOCALS = {
    dis.opmap[name]
    for name in _LOCAL_NAMES
    if any(name.startswith(base + "_") and name[len(base) + 1 :] in _LOCAL_NAMES for base in _LOCAL_NAMES)
}


def operand(op):
    """A syntactically valid argument for `op`, in the kind it expects."""
    if op in dis.hasjrel or op in getattr(dis, "hasjabs", ()):
        return "@target"
    if op in dis.hasconst:
        return '"k"'
    if op in dis.haslocal and op not in PACKED_LOCALS:
        return "$x"
    if op in dis.hasname:
        return "$myname"
    if op in dis.hasfree:
        return "$c"
    if op in dis.hascompare:
        return "asm.Compare.LT"
    if op >= dis.HAVE_ARGUMENT:
        return "0"
    return ""


def assemble(name):
    """Assemble `name` on its own, and return (code, its instruction)."""
    asm = Assembly()

    asm.parse(
        "".join(f"        {line}\n" for line in PROLOGUE)
        + f"        {name.lower()} {operand(dis.opmap[name])}\n"
        + "    target:\n"
        + "        load_const None\n"
        + "        return_value\n"
    )

    code = asm.compile()
    # Located by position, not by name: the prologue is mostly LOAD_CONSTs, so
    # matching on the opname alone would find one of those instead.
    instrs = [i for i in dis.get_instructions(code) if i.opname != "CACHE"]
    return code, instrs[len(PROLOGUE)]


@pytest.mark.parametrize("name", ASSEMBLABLE)
def test_opcode_assembles(name):
    _, instr = assemble(name)
    assert instr.opname == name


@pytest.mark.parametrize("name", ASSEMBLABLE)
def test_opcode_argument_round_trips(name):
    """The argument comes back out of the assembled code as it went in."""
    op = dis.opmap[name]
    code, instr = assemble(name)

    if op in dis.hasconst:
        assert code.co_consts[instr.arg] == "k"
    elif op in dis.haslocal and op not in PACKED_LOCALS:
        assert instr.argval == "x"
    elif op in dis.hasfree:
        # Compared through argval rather than by indexing a table: from 3.11
        # the oparg is an index into localsplus (varnames + cells + frees), not
        # into co_cellvars + co_freevars, and dis knows the difference.
        assert instr.argval == "c"
    elif op in dis.hasname:
        # LOAD_GLOBAL (3.11+) and LOAD_ATTR (3.12+) pack a flag into bit 0, so
        # the index is shifted; dis exposes the decoded name either way.
        assert instr.argval == "myname"
    elif op in dis.hascompare:
        assert instr.argrepr.startswith("<")
    elif op in dis.hasjrel or op in getattr(dis, "hasjabs", ()):
        # The label sits right after this instruction, so the jump has to
        # resolve to the offset of the LOAD_CONST that follows it.
        assert instr.argval > instr.offset


@pytest.mark.skipif(not INTERNAL, reason="no interpreter-internal opcodes at this version")
@pytest.mark.parametrize("name", INTERNAL)
def test_internal_opcode_rejected(name):
    """Opcodes only the interpreter may write are refused, not encoded.

    Some of them make PyCode_New dereference monitoring state that a fresh code
    object doesn't have, so assembling one is a segfault rather than an
    exception — worth refusing explicitly rather than passing through.
    """
    asm = Assembly()

    with pytest.raises(Exception, match="internal to the interpreter"):
        asm.parse(f"        {name.lower()} 0\n")


def test_internal_opcode_rejected_by_the_core():
    """The same guard applies to the core, which the assembler is only one user of."""
    from spasm.bytecode import Bytecode
    from spasm.bytecode import Instr

    # Pseudo-opcodes are all above 255 and so cannot be held by an Instr at
    # all; the ones worth guarding against are the instrumented family, which
    # fit in a byte and are what makes PyCode_New crash.
    min_instrumented = getattr(_opcode, "MIN_INSTRUMENTED_OPCODE", 1 << 30)
    ops = [dis.opmap[n] for n in INTERNAL if min_instrumented <= dis.opmap[n] <= 255]
    if not ops:
        pytest.skip("no instrumented opcodes at this version")
    op = ops[0]

    code = Bytecode([Instr("RESUME", 0), Instr(op), Instr("LOAD_CONST", None), Instr("RETURN_VALUE")])
    with pytest.raises(ValueError, match="internal to the interpreter"):
        code.to_code()


@pytest.mark.skipif(PY < (3, 12), reason="sys.monitoring instrumentation added in 3.12")
def test_monitored_function_still_round_trips():
    """The guard must not fire on code that is currently being monitored.

    Instrumenting is the main reason to reach for this library, so a code
    object with monitoring active has to keep round-tripping. It does because
    co_code hands back the de-instrumented bytes — this pins that down, since
    if it ever stopped the guard would turn it into a hard failure.
    """
    from spasm.bytecode import Bytecode

    def f(a):
        return a + 1

    tool = sys.monitoring.DEBUGGER_ID
    sys.monitoring.use_tool_id(tool, "spasm-test")
    try:
        sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, lambda *_: None)
        sys.monitoring.set_local_events(tool, f.__code__, sys.monitoring.events.LINE)

        code = Bytecode.from_code(f.__code__).to_code()
        assert code.co_code == f.__code__.co_code
        assert type(f)(code, {})(1) == 2
    finally:
        sys.monitoring.free_tool_id(tool)


def test_the_sweep_covers_the_instruction_set():
    """Guard against the filters above quietly emptying the sweep out."""
    assert len(ASSEMBLABLE) > 80
    # Nothing an interpreter actually executes should have been filtered out as
    # internal: the exclusions are monitoring and JIT bookkeeping only.
    assert not {"NOP", "RESUME", "LOAD_CONST", "RETURN_VALUE"} & set(INTERNAL)
    assert _opcode.opmap  # the tables come from the running interpreter
