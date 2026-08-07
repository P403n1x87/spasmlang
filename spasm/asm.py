# Use @ for labels
# Use % for try blocks
# use $ for string literals
# use # for comments
# use {} for bind opargs
# use () for arguments, [] for cellvars and <> for freevars, in a code header

# Grammar:
# ident                 ::= [a-zA-Z_][a-zA-Z0-9_]*
# number                ::= [0-9]+
# label                 ::= ident ":"
# label_ref             ::= "@" ident
# string_ref            ::= "$" ident
# try_block_begin       ::= "try" label_ref ["lasti"]?
# try_block_end         ::= "tried"
# opcode                ::= [A-Z][A-Z0-9_]*
# bind_opcode_arg       ::= "{" ident "}"
# opcode_arg            ::= label_ref | string | number | bind_opcode_arg | code_ref | ident["." ident]*
# instruction           ::= opcode [opcode_arg]?
# identlist             ::= [ident ["," ident]*]
# code_begin            ::= "code" ident "(" identlist ")" ["[" identlist "]"] ["<" identlist ">"]
# code_end              ::= "end"
# code_ref              ::= "." ident
# line                  ::= label | try_block_begin | try_block_end | code_begin | code_end | instruction

import dis
import re
import sys
import typing as t
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from types import CodeType

import spasm.bytecode
from spasm.bytecode import CO_NESTED
from spasm.bytecode import PY311
from spasm.bytecode import PY312
from spasm.bytecode import UNSET
from spasm.bytecode import Bytecode
from spasm.bytecode import Compare
from spasm.bytecode import ExcEntry
from spasm.bytecode import Instr
from spasm.bytecode import Label
from spasm.bytecode import compare_oparg
from spasm.bytecode import encode_name_arg
from spasm.bytecode import infer_flags
from spasm.bytecode import is_internal_op
from spasm.bytecode import is_name_op

_HASCOMPARE = frozenset(dis.hascompare)


class SpasmParseError(Exception):
    def __init__(self, filename: str, lineno: int) -> None:
        self.filename = filename
        self.lineno = lineno

    def __str__(self) -> str:
        return f"in {self.filename}, line {self.lineno}: {self.__cause__}"


def transform_instruction(opcode: str, arg: t.Any = UNSET) -> tuple[str, t.Any]:
    if PY312:
        if opcode.upper() == "LOAD_METHOD":
            opcode = "LOAD_ATTR"
            arg = (True, arg)
        elif opcode.upper() == "LOAD_ATTR" and not isinstance(arg, tuple):
            arg = (False, arg)

    return opcode, arg


# ---------------------------------------------------------------------------
# Parsed entries
#
# Parsing produces a flat list of instructions, mirroring the core's model:
# labels and try markers are not entries in that list but positions into it,
# recorded separately. Instruction arguments are kept in their abstract form (a
# name, a constant, a label reference) until bind() materialises a Bytecode,
# because resolving a name to an oparg needs the target code object's name
# table.
# ---------------------------------------------------------------------------


@dataclass
class LabelRef:
    """A reference to a label by name, resolved at bind time."""

    ident: str


@dataclass
class ExcEntryDef:
    """A protected region, as a half-open range of instruction indices."""

    start: int
    stop: int
    handler: str
    lasti: bool = False


@dataclass
class OpArg:
    """A parsed instruction, with its argument still in abstract form."""

    name: str
    arg: t.Any = UNSET
    lineno: int | None = None


class BaseOpArg(OpArg):
    """An instruction whose argument is only known at bind time."""

    def __call__(self, data: dict[str, t.Any], lineno: int | None = None) -> OpArg:
        raise NotImplementedError


class BindOpArg(BaseOpArg):
    def __call__(self, bind_args: dict[str, t.Any], lineno: int | None = None) -> OpArg:
        # The transformation is deferred along with the argument, since it can
        # depend on it.
        name, arg = transform_instruction(self.name, bind_args[self.arg])
        return OpArg(name, arg, lineno if lineno is not None else self.lineno)


class CodeRefOpArg(BaseOpArg):
    def __call__(self, codes: dict[str, CodeType], lineno: int | None = None) -> OpArg:
        name, arg = transform_instruction(self.name, codes[self.arg])
        return OpArg(name, arg, lineno if lineno is not None else self.lineno)


@dataclass
class CodeBegin:
    name: str
    args: list[str]
    cellvars: list[str] = field(default_factory=list)
    freevars: list[str] = field(default_factory=list)


class CodeEnd:
    pass


# code NAME(args)[cellvars]<freevars> — the two trailing groups are optional
# and independent, so `code f(x)<n>` declares a free variable and no cells.
# Each gets its own delimiter so that a header can name one without the other.
_CODE_BEGIN = re.compile(
    r"^code\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<args>[^)]*)\)"
    r"(?:\s*\[(?P<cellvars>[^\]]*)\])?"
    r"(?:\s*<(?P<freevars>[^>]*)>)?"
    r"\s*$"
)


class Assembly:
    def __init__(
        self,
        name: str | None = None,
        filename: str | None = None,
        lineno: int | None = None,
        *,
        is_function: bool = False,
        is_nested: bool = False,
    ) -> None:
        # Labels and protected regions are positions into the instruction
        # list, not entries in it, which is how the core models them too.
        self._labels: dict[str, int] = {}
        self._ref_labels: set[str] = set()
        self._exc_entries: list[ExcEntryDef] = []
        # The currently open try block, as (handler ident, lasti, start index).
        self._tb: tuple[str, bool, int] | None = None
        self._instrs: list[OpArg] = []
        self._name = name or "<assembly>"
        self._filename = filename or __file__
        self._lineno = lineno
        self._is_function = is_function
        self._is_nested = is_nested
        self._argnames: list[str] = []
        # Variables this code object closes over: cells are locals captured by
        # a nested block, frees are captured from the enclosing one. Declared
        # in the block header rather than inferred, since nothing in the
        # instruction stream distinguishes the two.
        self._cellvars: list[str] = []
        self._freevars: list[str] = []
        self._bind_opargs: dict[int, BindOpArg] = {}
        self._codes: dict[str, Assembly] = {}
        self._code_refs: dict[int, CodeRefOpArg] = {}

    # -- parsing ------------------------------------------------------------

    def _parse_ident(self, text: str) -> str:
        if not text.isidentifier():
            msg = f"invalid identifier {text}"
            raise ValueError(msg)

        return text

    def _parse_number(self, text: str) -> int | None:
        try:
            return int(text)
        except ValueError:
            return None

    def _parse_label(self, line: str) -> bool:
        """Bind a label to the position of the next instruction emitted."""
        if not line.endswith(":"):
            return False

        label_ident = self._parse_ident(line[:-1])
        if label_ident in self._labels:
            msg = f"label {label_ident} already defined"
            raise ValueError(msg)

        self._labels[label_ident] = len(self._instrs)
        self._ref_labels.discard(label_ident)

        return True

    def _parse_label_ref(self, text: str) -> LabelRef | None:
        if not text.startswith("@"):
            return None

        label_ident = self._parse_ident(text[1:])
        if label_ident not in self._labels:
            self._ref_labels.add(label_ident)

        return LabelRef(label_ident)

    def _parse_string_ref(self, text: str) -> str | None:
        if not text.startswith("$"):
            return None

        return text[1:]

    def _parse_try_begin(self, line: str) -> bool:
        """Open a protected region at the next instruction emitted."""
        try:
            head, label_ref, *lasti = line.split(maxsplit=2)
        except ValueError:
            return False

        if head != "try":
            return False

        if not PY311:
            msg = "try blocks require Python 3.11 or later (no exception table before then)"
            raise ValueError(msg)

        if self._tb is not None:
            msg = "cannot start try block while another is open"
            raise ValueError(msg)

        label = self._parse_label_ref(label_ref)
        if label is None:
            msg = "invalid label reference for try block"
            raise ValueError(msg)

        self._tb = (label.ident, bool(lasti), len(self._instrs))

        return True

    def _parse_try_end(self, line: str) -> bool:
        """Close the open protected region before the next instruction."""
        if line != "tried":
            return False

        if self._tb is None:
            msg = "cannot end try block while none is open"
            raise ValueError(msg)

        handler, lasti, start = self._tb
        self._exc_entries.append(ExcEntryDef(start, len(self._instrs), handler, lasti))
        self._tb = None

        return True

    def _parse_opcode(self, text: str) -> str:
        # The name is validated post-transformation (LOAD_METHOD is gone from
        # 3.12 onwards, but we still accept it and rewrite it) and returned
        # pre-transformation, since transforming needs the argument too.
        opcode = text.upper()
        resolved = transform_instruction(opcode)[0]
        if opcode not in dis.opmap and resolved not in dis.opmap:
            msg = f"unknown opcode {opcode}"
            raise ValueError(msg)
        # Checked on the resolved name: LOAD_METHOD is a pseudo-opcode from
        # 3.12 on, but we accept it as a spelling of LOAD_ATTR and so must not
        # turn it away here.
        if is_internal_op(resolved):
            msg = f"opcode {opcode} is internal to the interpreter and cannot be assembled"
            raise ValueError(msg)

        return opcode

    def _parse_expr(self, text: str) -> t.Any:
        frame = sys._getframe(1)
        _globals = frame.f_globals.copy()
        # `asm` exposes the low-level layer, so that symbolic opargs can be
        # written as e.g. `asm.Compare.NE` or `asm.BinaryOp.ADD`.
        _globals["asm"] = spasm.bytecode
        return eval(text, _globals, frame.f_locals)  # noqa: S307

    def _parse_opcode_arg(self, text: str) -> t.Any:
        return (
            self._parse_label_ref(text)
            or self._parse_string_ref(text)
            or self._parse_number(text)
            or self._parse_expr(text)
        )

    def _parse_bind_opcode_arg(self, text: str) -> str | None:
        if not text.startswith("{") or not text.endswith("}"):
            return None

        return text[1:-1]

    def _parse_code_ref_arg(self, text: str) -> str | None:
        if not text.startswith("."):
            return None

        return text[1:]

    def _parse_instruction(self, line: str) -> OpArg | None:
        opcode, *args = line.split(maxsplit=1)

        if args:
            (arg,) = args

            bind_arg = self._parse_bind_opcode_arg(arg)
            if bind_arg is not None:
                bind_entry = BindOpArg(self._parse_opcode(opcode), bind_arg, self._lineno)

                # TODO: What happens if a bind arg occurs multiple times?
                self._bind_opargs[len(self._instrs)] = bind_entry

                return bind_entry

            code_ref = self._parse_code_ref_arg(arg)
            if code_ref is not None:
                code_entry = CodeRefOpArg(self._parse_opcode(opcode), code_ref, self._lineno)

                self._code_refs[len(self._instrs)] = code_entry

                return code_entry

        name, arg = transform_instruction(self._parse_opcode(opcode), *map(self._parse_opcode_arg, args))

        return OpArg(name, arg, self._lineno)

    def _parse_ident_list(self, text: str | None) -> list[str]:
        if text is None or not text.strip():
            return []

        return [self._parse_ident(ident.strip()) for ident in text.split(",")]

    def _parse_code_begin(self, line: str) -> CodeBegin | None:
        if line != "code" and not line.startswith("code "):
            return None

        match = _CODE_BEGIN.match(line)
        if match is None:
            # It opens with `code`, so it was meant to be a block header;
            # falling through to the instruction parser would report it as an
            # unknown opcode instead.
            msg = f"invalid code block header: {line}"
            raise ValueError(msg)

        return CodeBegin(
            match["name"],
            self._parse_ident_list(match["args"]),
            self._parse_ident_list(match["cellvars"]),
            self._parse_ident_list(match["freevars"]),
        )

    def _parse_code_end(self, line: str) -> CodeEnd | None:
        return CodeEnd() if line == "end" else None

    def _parse_line(self, line: str) -> OpArg | CodeBegin | CodeEnd | None:
        """Parse a line into an entry, or ``None`` if it doesn't produce one.

        Labels and try markers don't: they record a position into the
        instruction list and are folded into the labels and exception entries
        at bind time.
        """
        if self._parse_label(line) or self._parse_try_begin(line) or self._parse_try_end(line):
            return None

        return self._parse_code_begin(line) or self._parse_code_end(line) or self._parse_instruction(line)

    def _validate(self) -> None:
        if self._ref_labels:
            undefined = ", ".join(sorted(self._ref_labels))
            msg = f"undefined labels: {undefined}"
            raise ValueError(msg)
        if self._tb is not None:
            msg = "unterminated try block"
            raise ValueError(msg)

        both = set(self._cellvars) & set(self._freevars)
        if both:
            names = ", ".join(sorted(both))
            msg = f"variables declared both cell and free: {names}"
            raise ValueError(msg)
        # A free variable comes from the enclosing scope, so it cannot also be
        # one of this block's own parameters.
        shadowed = set(self._freevars) & set(self._argnames)
        if shadowed:
            names = ", ".join(sorted(shadowed))
            msg = f"free variables shadow arguments: {names}"
            raise ValueError(msg)

    def _parse(self, lines: Iterator[tuple[int, str]], *, terminated: bool = False) -> None:
        """Consume ``lines`` until this block ends.

        The iterator is shared with every enclosing block: a nested ``code``
        header hands the same iterator to the child, which stops at its own
        ``end`` and leaves the rest for whoever is above it. ``terminated``
        says whether an ``end`` is expected — the outermost block runs to the
        end of the file instead.
        """
        for n, line in lines:
            try:
                entry = self._parse_line(line)
            except Exception as e:
                raise SpasmParseError(self._filename, n) from e

            if entry is None:
                continue

            if isinstance(entry, CodeBegin):
                if entry.name in self._codes:
                    msg = f"duplicate code block {entry.name}"
                    raise ValueError(msg)

                code = self._codes[entry.name] = Assembly(
                    name=entry.name,
                    filename=self._filename,
                    lineno=self._lineno,
                    is_function=True,
                    # A block nested inside a function is a nested scope; one
                    # nested directly in the module is not.
                    is_nested=self._is_function,
                )
                code._argnames = entry.args
                code._cellvars = entry.cellvars
                code._freevars = entry.freevars

                code._parse(lines, terminated=True)

                continue

            if isinstance(entry, CodeEnd):
                if not terminated:
                    msg = "code end outside of code block"
                    raise ValueError(msg)
                break

            entry.lineno = n
            self._instrs.append(entry)

        else:
            if terminated:
                msg = f"code block {self._name} not terminated"
                raise ValueError(msg)

        self._validate()

    def parse(self, text: str) -> None:
        self._parse(
            (n, _)
            for n, _ in ((n, _.strip()) for n, _ in enumerate(text.splitlines(), start=1))
            if _ and not _.startswith("#")
        )

    # -- materialisation ----------------------------------------------------

    def _resolve_arg(
        self,
        code: Bytecode,
        labels: dict[str, Label],
        name: str,
        arg: t.Any,
    ) -> t.Any:
        """Turn an abstract argument into what :class:`Instr` wants.

        Constant, local and free arguments are passed straight through — the
        core accepts those in abstract form and resolves them itself. Name and
        comparison arguments need encoding, which :mod:`spasm.bytecode` does.
        """
        if isinstance(arg, LabelRef):
            return labels[arg.ident]

        if arg is UNSET:
            return 0

        op = dis.opmap[name]

        if is_name_op(op):
            flag, ident = arg if isinstance(arg, tuple) else (False, arg)
            return encode_name_arg(code, name, ident, flag=flag)

        if op in _HASCOMPARE and isinstance(arg, Compare):
            return compare_oparg(arg)

        return arg

    def _materialise(self, entries: list[OpArg], lineno: int | None = None) -> Bytecode:
        code = Bytecode()
        code.name = code.qualname = self._name
        code.filename = self._filename
        code.firstlineno = lineno if lineno is not None else (self._lineno or 1)
        code.argcount = len(self._argnames)
        code.varnames = list(self._argnames)
        # Declared before any instruction is built: a free or cell argument is
        # resolved against these tables as it is encoded, and a name missing
        # from them would be taken for a new local instead.
        code.cellvars = list(self._cellvars)
        code.freevars = list(self._freevars)

        # The core attaches labels to the instruction they precede, so the
        # parsed positions become a per-index list of labels to hand over.
        attached: dict[int, list[Label]] = {}

        def label_at(index: int) -> Label:
            label = code.new_label()
            attached.setdefault(index, []).append(label)
            return label

        labels = {ident: label_at(index) for ident, index in self._labels.items()}

        # A protected region's bounds are positions in the stream just like
        # labels are, only anonymous.
        exc_entries = [
            # depth is left unset: the core infers it from the minimum stack
            # depth across the protected range.
            ExcEntry(label_at(e.start), label_at(e.stop), labels[e.handler], lasti=e.lasti)
            for e in self._exc_entries
        ]

        instrs: list[Instr] = []
        for index, entry in enumerate(entries):
            instr = Instr(
                entry.name,
                self._resolve_arg(code, labels, entry.name, entry.arg),
                lineno=lineno if lineno is not None else (entry.lineno or -1),
            )
            if index in attached:
                instr.labels = attached[index]
            instrs.append(instr)

        code.instrs = instrs
        # Labels with no instruction left to attach to point one past the end.
        code.end_labels = attached.get(len(entries), [])
        code.exc_entries = exc_entries

        # Inferred last: it depends on the instruction stream (to spot a
        # generator) and on the free/cell variables.
        code.flags = infer_flags(code, is_function=self._is_function)
        if self._is_nested:
            code.flags |= CO_NESTED

        return code

    def bind(self, args: dict[str, t.Any] | None = None, lineno: int | None = None) -> Bytecode:
        entries = self._instrs

        if self._bind_opargs or self._code_refs:
            missing_bind_args = {_.arg for _ in self._bind_opargs.values()} - set(args or {})
            if missing_bind_args:
                missing = ", ".join(sorted(missing_bind_args))
                msg = f"missing bind args: {missing}"
                raise ValueError(msg)

            # The parsed bytecode has BindOpArg/CodeRefOpArg placeholders in
            # it; make a copy with those resolved.
            entries = list(entries)
            for i, bind_arg in self._bind_opargs.items():
                entries[i] = bind_arg(t.cast(dict[str, t.Any], args), lineno=lineno)

            if self._code_refs:
                codes = {name: code.compile(args, lineno) for name, code in self._codes.items()}
                for i, code_ref in self._code_refs.items():
                    entries[i] = code_ref(codes, lineno=lineno)

        return self._materialise(entries, lineno=lineno)

    def compile(
        self,
        bind_args: dict[str, t.Any] | None = None,
        lineno: int | None = None,
    ) -> CodeType:
        return self.bind(bind_args, lineno=lineno).to_code()

    def dis(self) -> None:
        # Labels and try markers are positions, so they get printed back out
        # by index rather than iterated alongside the instructions.
        idents: dict[int, list[str]] = {}
        for ident, index in self._labels.items():
            idents.setdefault(index, []).append(ident)

        opened: dict[int, list[ExcEntryDef]] = {}
        closed: dict[int, list[ExcEntryDef]] = {}
        for exc_entry in self._exc_entries:
            opened.setdefault(exc_entry.start, []).append(exc_entry)
            closed.setdefault(exc_entry.stop, []).append(exc_entry)

        def markers(index: int) -> None:
            for _ in closed.get(index, ()):
                print("tried")  # noqa: T201
            for ident in idents.get(index, ()):
                print(f"{ident}:")  # noqa: T201
            for exc_entry in opened.get(index, ()):
                print(f"try @{exc_entry.handler} (lasti={exc_entry.lasti})")  # noqa: T201

        for index, entry in enumerate(self._instrs):
            markers(index)
            if isinstance(entry, BindOpArg):
                print(f"    {entry.name:<32}{{{entry.arg}}}")  # noqa: T201
            elif isinstance(entry, CodeRefOpArg):
                print(f"    {entry.name:<32}.{entry.arg}")  # noqa: T201
            else:
                arg = entry.arg
                print(f"    {entry.name:<32}{'' if arg is UNSET else arg}")  # noqa: T201

        markers(len(self._instrs))

    def __iter__(self) -> Iterator[OpArg]:
        return iter(self._instrs)
