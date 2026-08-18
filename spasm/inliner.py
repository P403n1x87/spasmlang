"""Splicing eligible callees directly into a caller's bytecode.

:func:`inline` is a decorator applied to the *caller*. It looks for
``LOAD_GLOBAL``-resolved calls with a fixed number of simple positional
arguments (plain ``LOAD_FAST``/``LOAD_CONST`` pushes, nothing computed
through a nested call or attribute access), and where the resolved callee is
a plain function it can prove safe to inline — no generator/coroutine, no
``*args``/``**kwargs``, no closure, no exception table, and an exact arg
count match — it splices the callee's body directly into the caller in place
of the call. Anything it can't prove safe is left exactly as it was: this is
a pure optimization, never a correctness risk for the calls it declines to
touch.
"""

import dis
import types
import typing as t

from spasm._core import Bytecode
from spasm._core import Instr
from spasm._core import Label
from spasm.bytecode import CO_ASYNC_GENERATOR
from spasm.bytecode import CO_COROUTINE
from spasm.bytecode import CO_GENERATOR
from spasm.bytecode import CO_VARARGS
from spasm.bytecode import CO_VARKEYWORDS
from spasm.bytecode import PY311
from spasm.bytecode import PY312
from spasm.bytecode import decode_name_arg
from spasm.bytecode import encode_name_arg
from spasm.bytecode import is_name_op

__all__ = ["inline"]

_F = t.TypeVar("_F", bound=t.Callable[..., t.Any])

# Instructions allowed in the "push arguments" run between the callable load
# and the call itself: single-value, side-effect-free pushes only. A nested
# call, attribute access or jump in there takes the call site out of scope.
_SIMPLE_ARG_OPS = frozenset(
    name for name in ("LOAD_FAST", "LOAD_CONST", "LOAD_FAST_CHECK", "LOAD_FAST_BORROW") if name in dis.opmap
)

_HASLOCAL = frozenset(dis.haslocal)

# 3.13+ fuses two adjacent local accesses into one instruction whose oparg
# packs both varname indices as (idx1 << 4) | idx2 (see CPython's
# compile.c). from_code() doesn't unpack that, so it's decoded here and
# split back into the two plain instructions it came from.
_PAIRED_LOCAL_OPS = {
    name: pair
    for name, pair in (
        ("LOAD_FAST_LOAD_FAST", ("LOAD_FAST", "LOAD_FAST")),
        ("STORE_FAST_LOAD_FAST", ("STORE_FAST", "LOAD_FAST")),
        ("STORE_FAST_STORE_FAST", ("STORE_FAST", "STORE_FAST")),
    )
    if name in dis.opmap
}

# LOAD_FAST_AND_CLEAR (comprehension-only) doesn't fit the abstraction model
# either and has no simple unpacking; a callee containing one is left alone.
_DISALLOWED_CALLEE_OPNAMES = frozenset(name for name in ("LOAD_FAST_AND_CLEAR",) if name in dis.opmap)

_INELIGIBLE_CALLEE_FLAGS = CO_GENERATOR | CO_COROUTINE | CO_ASYNC_GENERATOR | CO_VARARGS | CO_VARKEYWORDS

# The call-site terminator: the opcode(s) that must immediately follow the
# argument-push run, each carrying the positional argument count as its arg.
if PY312:
    _CALL_TERMINATOR: tuple[str, ...] = ("CALL",)
elif PY311:
    _CALL_TERMINATOR = ("PRECALL", "CALL")
else:
    _CALL_TERMINATOR = ("CALL_FUNCTION",)


class _CallSite(t.NamedTuple):
    start: int  # index of the LOAD_GLOBAL loading the callable
    end: int  # one past the last terminator instruction
    name: str
    argcount: int  # number of positional values pushed
    arg_instr_count: int  # number of instructions doing the pushing (<= argcount)


def _decode_global(names: t.Sequence[str], instr: Instr) -> tuple[str, bool] | None:
    if dis.opname[instr.op] != "LOAD_GLOBAL":
        return None
    return decode_name_arg(names, "LOAD_GLOBAL", instr.arg)


def _match_call_site(instrs: list[Instr], names: t.Sequence[str], start: int) -> _CallSite | None:
    decoded = _decode_global(names, instrs[start])
    if decoded is None:
        return None
    name, has_null = decoded
    if PY311 and not has_null:
        # A plain global load, not the callable position of a call.
        return None

    idx = start + 1
    argcount = 0
    while idx < len(instrs) and dis.opname[instrs[idx].op] != _CALL_TERMINATOR[0]:
        instr = instrs[idx]
        op_name = dis.opname[instr.op]
        if instr.labels:
            return None
        if op_name in _SIMPLE_ARG_OPS:
            argcount += 1
        elif op_name == "LOAD_FAST_LOAD_FAST":
            # Pure double-push, no side effect — safe in an arg-push run.
            argcount += 2
        else:
            return None
        idx += 1

    if idx >= len(instrs):
        return None

    arg_instr_count = idx - (start + 1)
    end = idx
    for term_name in _CALL_TERMINATOR:
        if end >= len(instrs):
            return None
        instr = instrs[end]
        if dis.opname[instr.op] != term_name or instr.arg != argcount or instr.labels:
            return None
        end += 1

    return _CallSite(start=start, end=end, name=name, argcount=argcount, arg_instr_count=arg_instr_count)


def _find_call_sites(bc: Bytecode) -> list[_CallSite]:
    sites = []
    instrs = bc.instrs
    i = 0
    while i < len(instrs):
        if dis.opname[instrs[i].op] == "LOAD_GLOBAL":
            site = _match_call_site(instrs, bc.names, i)
            if site is not None:
                sites.append(site)
                i = site.end
                continue
        i += 1
    return sites


def _resolve_global(func: types.FunctionType, name: str) -> t.Any:
    globals_ = func.__globals__
    if name in globals_:
        return globals_[name]
    builtins_ = globals_.get("__builtins__")
    if isinstance(builtins_, dict):
        return builtins_.get(name)
    return getattr(builtins_, name, None)


def _eligible_callee(callee: t.Any, caller: types.FunctionType, argcount: int) -> types.CodeType | None:
    if not isinstance(callee, types.FunctionType) or callee.__code__ is caller.__code__:
        return None

    code = callee.__code__
    if code.co_flags & _INELIGIBLE_CALLEE_FLAGS:
        return None
    if code.co_kwonlyargcount or code.co_argcount != argcount:
        return None
    if code.co_freevars or code.co_cellvars:
        return None

    return code


def _arg_push_sources(arg_instrs: list[Instr], argcount: int) -> list[Instr | None]:
    """Map each pushed-argument slot to the single instruction that pushes it.

    ``None`` for a slot means it's one half of a fused ``LOAD_FAST_LOAD_FAST``
    push: two values from one instruction, so there's no single instruction
    to duplicate for that slot alone.
    """
    sources: list[Instr | None] = [None] * argcount
    slot = 0
    for instr in arg_instrs:
        if dis.opname[instr.op] == "LOAD_FAST_LOAD_FAST":
            slot += 2
        else:
            sources[slot] = instr
            slot += 1
    return sources


def _reassigned_params(callee_bc: Bytecode, argcount: int) -> list[bool]:
    """Which of the callee's first ``argcount`` locals are ever written to.

    A parameter that's never reassigned can have its reads replaced by a
    fresh copy of whatever pushed it in the caller, instead of round-tripping
    through a dedicated ``STORE_FAST``/``LOAD_FAST`` pair.
    """
    name_to_index = {callee_bc.varnames[i]: i for i in range(argcount)}
    reassigned = [False] * argcount

    for instr in callee_bc.instrs:
        op_name = dis.opname[instr.op]
        if op_name in ("STORE_FAST", "DELETE_FAST"):
            idx = name_to_index.get(instr.arg)
            if idx is not None:
                reassigned[idx] = True
        elif op_name in _PAIRED_LOCAL_OPS:
            op1, op2 = _PAIRED_LOCAL_OPS[op_name]
            idx1, idx2 = instr.arg >> 4, instr.arg & 0xF
            if op1 == "STORE_FAST" and idx1 < argcount:
                reassigned[idx1] = True
            if op2 == "STORE_FAST" and idx2 < argcount:
                reassigned[idx2] = True

    return reassigned


def _splice(
    caller_bc: Bytecode,
    callee_code: types.CodeType,
    call_site: _CallSite,
    splice_id: int,
    arg_instrs: list[Instr],
) -> tuple[list[Instr], list[Instr], Label] | None:
    callee_bc = Bytecode.from_code(callee_code)
    if callee_bc.exc_entries:
        return None
    if any(dis.opname[instr.op] in _DISALLOWED_CALLEE_OPNAMES for instr in callee_bc.instrs):
        return None

    argcount = call_site.argcount
    varname_map = {name: f"__inline_{splice_id}_{name}" for name in callee_bc.varnames}
    end_label = caller_bc.new_label()
    label_map: dict[Label, Label] = dict.fromkeys(callee_bc.end_labels, end_label)

    def mapped_label(label: Label) -> Label:
        if label not in label_map:
            label_map[label] = caller_bc.new_label()
        return label_map[label]

    # A parameter that's read-only in the callee and pushed by a single,
    # side-effect-free instruction in the caller doesn't need a dedicated
    # local at all: every read of it is replaced by a fresh copy of that
    # push, and the push/store pair that would otherwise ferry it through a
    # local is dropped entirely.
    arg_sources = _arg_push_sources(arg_instrs, argcount)
    reassigned = _reassigned_params(callee_bc, argcount)
    propagate = [arg_sources[i] is not None and not reassigned[i] for i in range(argcount)]
    name_to_index = {callee_bc.varnames[i]: i for i in range(argcount)}

    def local_read(op_name: str, idx: int, lineno: int) -> Instr:
        source = arg_sources[idx] if idx < argcount else None
        if op_name.startswith("LOAD_FAST") and source is not None and not reassigned[idx]:
            return Instr(dis.opname[source.op], source.arg, lineno=lineno)
        return Instr(op_name, varname_map[callee_bc.varnames[idx]], lineno=lineno)

    kept_arg_instrs: list[Instr] = []
    slot = 0
    for instr in arg_instrs:
        if dis.opname[instr.op] == "LOAD_FAST_LOAD_FAST":
            kept_arg_instrs.append(instr)
            slot += 2
        else:
            if not propagate[slot]:
                kept_arg_instrs.append(instr)
            slot += 1

    lineno = caller_bc.instrs[call_site.end - 1].lineno

    spliced: list[Instr] = [
        Instr("STORE_FAST", varname_map[callee_bc.varnames[i]], lineno=lineno)
        for i in range(argcount - 1, -1, -1)
        if not propagate[i]
    ]

    # RESUME only makes sense as the very first instruction of a code
    # object (frame setup); the caller already executed its own. Drop it,
    # carrying forward any label pointing at it onto the next instruction.
    pending_labels: list[Label] = []

    for instr in callee_bc.instrs:
        op_name = dis.opname[instr.op]
        new_labels = pending_labels + [mapped_label(label) for label in instr.labels]
        pending_labels = []

        if op_name == "RESUME":
            pending_labels = new_labels
            continue

        if op_name in _PAIRED_LOCAL_OPS:
            op1, op2 = _PAIRED_LOCAL_OPS[op_name]
            idx1, idx2 = instr.arg >> 4, instr.arg & 0xF
            new = [
                local_read(op1, idx1, instr.lineno),
                local_read(op2, idx2, instr.lineno),
            ]
            new[0].labels = new_labels
            spliced.extend(new)
            continue

        if op_name == "RETURN_CONST":
            # RETURN_CONST pushes-and-returns a constant in one instruction
            # (3.12+); splitting it back into the push and the jump it
            # stands in for needs two.
            new = [
                Instr("LOAD_CONST", instr.arg, lineno=instr.lineno),
                Instr("JUMP_FORWARD", end_label, lineno=instr.lineno),
            ]
            new[0].labels = new_labels
            spliced.extend(new)
            continue

        if op_name == "RETURN_VALUE":
            new_instr = Instr("JUMP_FORWARD", end_label, lineno=instr.lineno)
        elif instr.op in _HASLOCAL:
            idx = name_to_index.get(instr.arg)
            if idx is not None:
                new_instr = local_read(op_name, idx, instr.lineno)
            else:
                new_instr = Instr(op_name, varname_map[instr.arg], lineno=instr.lineno)
        elif is_name_op(instr.op):
            name, flag = decode_name_arg(callee_bc.names, op_name, instr.arg)
            new_instr = Instr(op_name, encode_name_arg(caller_bc, op_name, name, flag=flag), lineno=instr.lineno)
        elif isinstance(instr.arg, Label):
            new_instr = Instr(op_name, mapped_label(instr.arg), lineno=instr.lineno)
        else:
            new_instr = Instr(op_name, instr.arg, lineno=instr.lineno)

        new_instr.labels = new_labels
        spliced.append(new_instr)

    return kept_arg_instrs, spliced, end_label


def inline(func: _F) -> _F:
    """Splice safe, module-level calls made by ``func`` in place of the call.

    Only calls resolved via ``LOAD_GLOBAL`` with a fixed number of simple
    positional arguments are considered; calls this can't prove safe (kwargs,
    starred args, method/attribute calls, generator or closure callees,
    recursive calls, ...) are left exactly as they were.
    """
    code = func.__code__
    bc = Bytecode.from_code(code)
    sites = {site.start: site for site in _find_call_sites(bc)}
    if not sites:
        return func

    instrs = bc.instrs
    new_instrs: list[Instr] = []
    pending_labels: list[Label] = []
    splice_id = 0
    changed = False
    func_ = t.cast("types.FunctionType", func)

    i = 0
    while i < len(instrs):
        site = sites.get(i)
        result = None
        if site is not None:
            callee_code = _eligible_callee(_resolve_global(func_, site.name), func_, site.argcount)
            if callee_code is not None:
                arg_instrs = instrs[site.start + 1 : site.start + 1 + site.arg_instr_count]
                result = _splice(bc, callee_code, site, splice_id, arg_instrs)

        if site is None or result is None:
            instr = instrs[i]
            if pending_labels:
                instr.labels = pending_labels + list(instr.labels)
                pending_labels = []
            new_instrs.append(instr)
            i += 1
            continue

        kept_arg_instrs, spliced, end_label = result
        splice_id += 1
        changed = True

        # The original argument-push instructions (between the LOAD_GLOBAL
        # and the CALL terminator) are kept as-is where their value still
        # needs to reach a local: they compute and push the values the
        # callee's STORE_FASTs below consume. Pushes that _splice inlined
        # directly at their use site instead are dropped here. Either way,
        # the LOAD_GLOBAL and the CALL/PRECALL/CALL_FUNCTION terminator
        # itself are always dropped.
        head = kept_arg_instrs[0] if kept_arg_instrs else spliced[0]
        head.labels = pending_labels + list(instrs[site.start].labels) + head.labels
        pending_labels = [end_label]

        new_instrs.extend(kept_arg_instrs)
        new_instrs.extend(spliced)
        i = site.end

    if not changed:
        return func

    if pending_labels:
        bc.end_labels = pending_labels + bc.end_labels

    bc.instrs = new_instrs
    func.__code__ = bc.to_code()  # type: ignore[misc]
    return func
