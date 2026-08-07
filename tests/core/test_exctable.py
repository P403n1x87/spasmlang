"""Tests for exception table round-trip and offset tracking."""

import dis
import sys
import types

import pytest

from spasm import _core

Bytecode = _core.Bytecode
Instr = _core.Instr
Label = _core.Label
ExcEntry = _core.ExcEntry
NOP = dis.opmap["NOP"]


def test_exc_entries_exposed():
    def f():
        try:
            return 1
        except Exception:
            return 0

    bc = Bytecode.from_code(f.__code__)
    assert isinstance(bc.exc_entries, list)

    if sys.version_info < (3, 11):
        # No exception table before 3.11; exc_entries is always empty.
        return

    assert len(bc.exc_entries) > 0
    for e in bc.exc_entries:
        assert isinstance(e, ExcEntry)
        assert isinstance(e.start, Label)
        assert isinstance(e.stop, Label)
        assert isinstance(e.handler, Label)
        assert isinstance(e.depth, int)
        assert isinstance(e.lasti, bool)


def test_exc_round_trip():
    """Plain from_code → to_code preserves try/except behaviour."""

    def safe_div(a, b):
        try:
            return a // b
        except ZeroDivisionError:
            return -1

    bc = Bytecode.from_code(safe_div.__code__)
    new_fn = types.FunctionType(bc.to_code(), safe_div.__globals__)
    assert new_fn(10, 2) == 5
    assert new_fn(10, 0) == -1


def test_exc_after_nop_insertion():
    """Insert NOPs and verify try/except still works correctly."""

    def safe_div(a, b):
        try:
            return a // b
        except ZeroDivisionError:
            return -1

    bc = Bytecode.from_code(safe_div.__code__)

    # Insert a NOP before every instruction. Every entry in .instrs is real
    # (no anchors) — labels stay attached to the Instr they target, so this
    # can't accidentally land a jump on the wrong instruction.
    for i in range(len(bc.instrs) - 1, -1, -1):
        bc.instrs.insert(i, Instr(NOP))

    new_fn = types.FunctionType(bc.to_code(), safe_div.__globals__)
    assert new_fn(10, 2) == 5
    assert new_fn(10, 0) == -1


def test_exc_labels_track_insertion():
    """Verify labels move correctly when instructions are inserted before them."""

    def f():
        try:
            x = 1
        except Exception:
            x = 0
        return x

    bc = Bytecode.from_code(f.__code__)

    start_ids_before = {e.start.id for e in bc.exc_entries}
    handler_ids_before = {e.handler.id for e in bc.exc_entries}

    # Insert 10 NOPs at the very beginning.
    for _ in range(10):
        bc.instrs.insert(0, Instr(NOP))

    # Label IDs are symbolic — they don't change.
    start_ids_after = {e.start.id for e in bc.exc_entries}
    handler_ids_after = {e.handler.id for e in bc.exc_entries}
    assert start_ids_before == start_ids_after
    assert handler_ids_before == handler_ids_after

    # The assembled code must still work.
    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    assert new_fn() == 1


def test_exc_entry_construction():
    """ExcEntry can be constructed from Python."""
    bc = Bytecode.from_code((lambda: None).__code__)
    l1 = bc.new_label()
    l2 = bc.new_label()
    l3 = bc.new_label()

    ee = ExcEntry(l1, l2, l3, depth=2, lasti=True)
    assert ee.start.id == l1.id
    assert ee.stop.id == l2.id
    assert ee.handler.id == l3.id
    assert ee.depth == 2
    assert ee.lasti is True


def test_nested_try_except():
    """Nested try/except survives round-trip."""

    def nested(x):
        try:
            try:
                return 10 // x
            except ZeroDivisionError:
                return -1
        except Exception:
            return -2

    bc = Bytecode.from_code(nested.__code__)
    new_fn = types.FunctionType(bc.to_code(), nested.__globals__)
    assert new_fn(2) == 5
    assert new_fn(0) == -1


def test_exc_depth_auto_computed():
    """An ExcEntry whose protected region normal control flow walks into has
    its depth inferred by to_code() as the region's minimum stack depth —
    which is exactly the base depth of the enclosing try block, i.e. what
    CPython itself records."""
    if sys.version_info < (3, 11):
        return  # no exception table before 3.11

    # The try body must be able to raise, or CPython emits no entry for it.
    def f(x):
        try:
            return 10 // x
        except ZeroDivisionError:
            return -1

    bc = Bytecode.from_code(f.__code__)
    # entry 0 guards the try body proper — the one region ordinary control
    # flow walks into. The rest are the handler's own cleanup chain.
    body_entry = bc.exc_entries[0]
    original_depth = body_entry.depth
    body_entry.depth = -1  # EXC_DEPTH_AUTO

    new_code = bc.to_code()
    new_fn = types.FunctionType(new_code, f.__globals__)
    assert new_fn(2) == 5
    assert new_fn(0) == -1
    assert Bytecode.from_code(new_code).exc_entries[0].depth == original_depth


def test_exc_depth_auto_rejects_cleanup_chain():
    """Auto-depth is exact-or-error. The depth CPython records is the
    block-nesting base depth of the enclosing try, which its compiler reads
    off SETUP_FINALLY/POP_BLOCK pseudo-ops that the assembler consumes. For a
    compiler-generated cleanup-chain entry — one reachable only by unwinding
    into a handler — that base is genuinely absent from the assembled table
    (the correct value can be 0 while every instruction in the region runs at
    depth 2+), so to_code() must refuse rather than emit a too-high depth."""
    if sys.version_info < (3, 11):
        return

    def nested(x):
        try:
            try:
                return 10 // x
            except ZeroDivisionError:
                return -1
        except Exception:
            return -2

    bc = Bytecode.from_code(nested.__code__)
    assert len(bc.exc_entries) > 1
    for e in bc.exc_entries:
        e.depth = -1  # EXC_DEPTH_AUTO

    with pytest.raises(ValueError, match="not recoverable"):
        bc.to_code()


def test_exc_depth_auto_hand_built_entry():
    """The case auto-depth exists for: an entry built by hand, guarding a
    region reached by ordinary control flow, with no depth supplied."""
    if sys.version_info < (3, 11):
        return

    def f(x):
        try:
            return 10 // x
        except ZeroDivisionError:
            return -1

    bc = Bytecode.from_code(f.__code__)
    body_entry = bc.exc_entries[0]
    rebuilt = ExcEntry(body_entry.start, body_entry.stop, body_entry.handler, lasti=body_entry.lasti)  # depth left AUTO
    assert rebuilt.depth == -1
    bc.exc_entries[0] = rebuilt

    new_fn = types.FunctionType(bc.to_code(), f.__globals__)
    assert new_fn(2) == 5
    assert new_fn(0) == -1


def test_exc_depth_auto_unreachable_start_raises():
    """An ExcEntry whose protected region is unreachable from normal
    control flow can't have its depth inferred — this must raise, not
    silently produce a broken exception table."""
    if sys.version_info < (3, 11):
        return

    bc = Bytecode.from_code((lambda: None).__code__)
    start = bc.new_label()  # never attached to any instruction
    stop = bc.new_label()
    handler = bc.new_label()
    bc.instrs[-1].labels.append(handler)
    bc.exc_entries.append(ExcEntry(start, stop, handler))  # depth left AUTO

    with pytest.raises(ValueError):
        bc.to_code()


if __name__ == "__main__":
    test_exc_entries_exposed()
    test_exc_round_trip()
    test_exc_after_nop_insertion()
    test_exc_labels_track_insertion()
    test_exc_entry_construction()
    test_nested_try_except()
    test_exc_depth_auto_computed()
    test_exc_depth_auto_rejects_cleanup_chain()
    test_exc_depth_auto_hand_built_entry()
    test_exc_depth_auto_unreachable_start_raises()
    print(f"All exception table tests passed (Python {sys.version})")
