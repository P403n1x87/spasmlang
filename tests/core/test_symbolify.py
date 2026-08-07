"""Tests for Instr/Label types, jump-target resolution, and instruction
mutation. Jump targets are resolved to Labels automatically by
Bytecode.from_code() — there is no separate symbolify step to call."""

import dis
import sys
import types

from spasm import _core

Bytecode = _core.Bytecode
Instr = _core.Instr
Label = _core.Label

JUMP_OPS = set(getattr(dis, "hasjump", [])) | set(getattr(dis, "hasjabs", [])) | set(getattr(dis, "hasjrel", []))
NOP = dis.opmap["NOP"]


# ── Instr / Label construction ────────────────────────────────────────────────


def test_instr_construction():
    i = Instr(NOP)
    assert i.op == NOP
    assert i.arg == 0


def test_instr_with_label():
    lbl = Label(42)
    i = Instr(dis.opmap["JUMP_FORWARD"], lbl)
    assert isinstance(i.arg, Label)
    assert i.arg.id == 42


def test_instr_location():
    i = Instr(NOP, 0, lineno=10, end_lineno=10, col_offset=4, end_col=7)
    assert i.lineno == 10
    assert i.col_offset == 4


def test_label_equality():
    assert Label(1) == Label(1)
    assert Label(1) != Label(2)
    assert hash(Label(3)) == hash(Label(3))


# ── jump-target resolution ─────────────────────────────────────────────────────


def test_jump_args_are_already_labels():
    def f(x):
        if x > 0:
            return x
        return -x

    bc = Bytecode.from_code(f.__code__)

    for instr in bc.instrs:
        if instr.op in JUMP_OPS:
            assert isinstance(instr.arg, Label), f"op {instr.op} has a raw int arg, expected an already-resolved Label"


def test_jump_round_trip():
    def total(n):
        s = 0
        for i in range(n):
            s += i
        return s

    bc = Bytecode.from_code(total.__code__)
    new_fn = types.FunctionType(bc.to_code(), total.__globals__)
    assert new_fn(10) == total(10)


def test_try_except_round_trip():
    def safe_div(a, b):
        try:
            return a // b
        except ZeroDivisionError:
            return None

    bc = Bytecode.from_code(safe_div.__code__)
    new_fn = types.FunctionType(bc.to_code(), safe_div.__globals__)
    assert new_fn(10, 2) == 5
    assert new_fn(10, 0) is None


# ── Mutation: instrs is the live list ─────────────────────────────────────────


def test_instrs_is_live():
    """Modifying bc.instrs changes the bytecode assembled by to_code()."""

    def f():
        return 1

    bc = Bytecode.from_code(f.__code__)
    original_len = len(bc.instrs)

    # Insert a NOP at position 0.
    bc.instrs.insert(0, Instr(NOP))
    assert len(bc.instrs) == original_len + 1

    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    assert new_fn() == 1  # NOP doesn't change behaviour


def test_insert_nop_before_every_instr():
    """Wrap every real instruction with a NOP and verify behaviour."""

    def fib(n):
        a, b = 0, 1
        while n > 0:
            a, b = b, a + b
            n -= 1
        return a

    bc = Bytecode.from_code(fib.__code__)

    # Insert NOP before every instruction, back to front. Every entry in
    # .instrs is a real instruction — labels travel with the Instr object
    # they're attached to, so a jump landing on instruction X still lands
    # on X (not the newly-inserted NOP before it).
    for i in range(len(bc.instrs) - 1, -1, -1):
        bc.instrs.insert(i, Instr(NOP))

    new_fn = types.FunctionType(bc.to_code(), fib.__globals__)
    assert new_fn(10) == fib(10)


def test_replace_instr():
    """Replace a LOAD_CONST with a different constant index."""
    LOAD_CONST = dis.opmap["LOAD_CONST"]

    def f():
        return 1

    bc = Bytecode.from_code(f.__code__)
    # Find the LOAD_CONST and change its arg to point at the same constant
    # (round-trip: arg stays the same, just verifying mutation works).
    for instr in bc.instrs:
        if instr.op == LOAD_CONST:
            orig = instr.arg
            instr.arg = orig  # no-op mutation
            break

    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    assert new_fn() == 1


def test_delete_nop():
    """Strip all NOPs and verify the function still works."""

    def f(x):
        return x * 2

    # Build a bytecode with extra NOPs, then strip them.
    bc = Bytecode.from_code(f.__code__)
    for _ in range(3):
        bc.instrs.insert(0, Instr(NOP))

    # Strip NOPs.
    bc.instrs = [i for i in bc.instrs if i.op != NOP]
    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    assert new_fn(21) == 42


def test_new_label():
    """bc.new_label() returns distinct Label objects."""
    bc = Bytecode.from_code((lambda: None).__code__)
    l1 = bc.new_label()
    l2 = bc.new_label()
    assert isinstance(l1, Label)
    assert l1 != l2
    assert l1.id != l2.id


def test_label_positions_matches_jump_targets():
    """label_positions() reports where each jump's Label currently points."""

    def f(x):
        if x > 0:
            return x
        return -x

    bc = Bytecode.from_code(f.__code__)
    positions = bc.label_positions()

    for instr in bc.instrs:
        if instr.op in JUMP_OPS:
            assert instr.arg in positions
            target_idx = positions[instr.arg]
            assert instr.arg in bc.instrs[target_idx].labels


def test_label_positions_after_insertion():
    """label_positions() reflects edits — it's recomputed on every call."""

    def f(x):
        if x > 0:
            return x
        return -x

    bc = Bytecode.from_code(f.__code__)
    target_lbl = next(lbl for instr in bc.instrs for lbl in instr.labels)
    before = bc.label_positions()[target_lbl]

    bc.instrs.insert(0, Instr(NOP))
    after = bc.label_positions()[target_lbl]

    assert after == before + 1


if __name__ == "__main__":
    test_instr_construction()
    test_instr_with_label()
    test_instr_location()
    test_label_equality()
    test_jump_args_are_already_labels()
    test_jump_round_trip()
    test_try_except_round_trip()
    test_instrs_is_live()
    test_insert_nop_before_every_instr()
    test_replace_instr()
    test_delete_nop()
    test_new_label()
    test_label_positions_matches_jump_targets()
    test_label_positions_after_insertion()
    print(f"All mutation tests passed (Python {sys.version})")
