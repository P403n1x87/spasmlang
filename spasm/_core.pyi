"""Type stubs for the ``spasm._core`` C++ extension.

Hand-written, since the extension is built from C++ and carries no annotations
of its own. Keep in sync with ``src/module.cpp``: the getset tables there are
the source of truth for the attributes, and the ``*_init`` functions for the
constructor signatures.
"""

import typing as t
from collections.abc import Sequence
from types import CodeType

PY_VERSION_HEX: int

class Label:
    """A symbolic position in an instruction list.

    Allocate one with :meth:`Bytecode.new_label` rather than constructing it
    directly; the id has to be unique within the ``Bytecode`` that uses it.
    """

    def __init__(self, id: int) -> None: ...  # noqa: A002
    @property
    def id(self) -> int: ...

class Instr:
    """A single bytecode instruction."""

    def __init__(
        self,
        op: int | str,
        arg: t.Any = ...,
        lineno: int = ...,
        end_lineno: int = ...,
        col_offset: int = ...,
        end_col: int = ...,
        labels: Sequence[Label] = ...,
    ) -> None: ...

    # Settable as an int or as an opname string; always reads back as an int.
    op: t.Any
    arg: t.Any
    lineno: int
    end_lineno: int
    col_offset: int
    end_col: int
    labels: list[Label]

class ExcEntry:
    """One exception table entry (3.11+)."""

    def __init__(
        self,
        start: Label,
        stop: Label,
        handler: Label,
        depth: int = ...,
        lasti: bool = ...,
    ) -> None: ...

    start: Label
    stop: Label
    handler: Label
    depth: int
    lasti: bool

class Bytecode:
    """A mutable, decoded code object."""

    def __init__(self, instrs: Sequence[Instr] = ...) -> None: ...
    @staticmethod
    def from_code(code: CodeType) -> Bytecode: ...
    def to_code(self) -> CodeType: ...
    def new_label(self) -> Label: ...
    def label_positions(self) -> dict[Label, int]: ...
    def add_const(self, obj: t.Any) -> int: ...
    def add_name(self, name: str) -> int: ...
    def add_varname(self, name: str) -> int: ...

    instrs: list[Instr]
    end_labels: list[Label]
    exc_entries: list[ExcEntry]
    consts: list[t.Any]
    names: list[str]
    varnames: list[str]
    freevars: list[str]
    cellvars: list[str]
    argcount: int
    flags: int
    firstlineno: int
    filename: str
    name: str
    qualname: str
