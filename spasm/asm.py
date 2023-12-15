# Use @ for labels
# Use % for try blocks
# use $ for string literals
# use # for comments
# use {} for bind opargs
# use [] for cellvars
# use () for freevars

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
# code_begin            ::= "code" ident "(" [ident ["," ident]] ")"
# code_end              ::= "end"
# code_ref              ::= "." ident
# line                  ::= label | try_block_begin | try_block_end | code_begin | code_end | instruction

import dis
import sys
import typing as t
from dataclasses import dataclass
from types import CodeType

import bytecode as bc  # type: ignore[import]


class SpasmParseError(Exception):
    def __init__(self, filename, lineno):
        self.filename = filename
        self.lineno = lineno

    def __str__(self):
        return f"in {self.filename}, line {self.lineno}: {self.__cause__}"


def relocate(instrs: bc.Bytecode, lineno: int) -> bc.Bytecode:
    new_instrs = bc.Bytecode()
    for i in instrs:
        if isinstance(i, bc.Instr):
            new_i = i.copy()
            new_i.lineno = lineno
            new_instrs.append(new_i)
        else:
            new_instrs.append(i)
    return new_instrs


def transform_instruction(opcode: str, arg: t.Any = bc.UNSET) -> t.Tuple[str, t.Any]:
    if sys.version_info >= (3, 12):
        if opcode.upper() == "LOAD_METHOD":
            opcode = "LOAD_ATTR"
            arg = (True, arg)
        elif opcode.upper() == "LOAD_ATTR" and not isinstance(arg, tuple):
            arg = (False, arg)

    return opcode, arg


class BaseOpArg(bc.Label):
    # We cannot have arbitrary objects in Bytecode, so we subclass Label
    def __init__(self, name: str, arg: str, lineno: t.Optional[int] = None) -> None:
        self.name = name
        self.arg = arg
        self.lineno = lineno

    def __call__(self, data: t.Dict[str, t.Any], lineno: t.Optional[int] = None) -> bc.Instr:
        raise NotImplementedError


class BindOpArg(BaseOpArg):
    def __call__(self, bind_args: t.Dict[str, t.Any], lineno: t.Optional[int] = None) -> bc.Instr:
        return bc.Instr(self.name, bind_args[self.arg], lineno=lineno if lineno is not None else self.lineno)


class CodeRefOpArg(BaseOpArg):
    def __call__(self, codes: t.Dict[str, CodeType], lineno: t.Optional[int] = None) -> bc.Instr:
        return bc.Instr(self.name, codes[self.arg], lineno=lineno if lineno is not None else self.lineno)


@dataclass
class CodeBegin:
    name: str
    args: t.List[str]


class CodeEnd:
    pass


class Assembly:
    def __init__(
        self, name: t.Optional[str] = None, filename: t.Optional[str] = None, lineno: t.Optional[int] = None
    ) -> None:
        self._labels: t.Dict[str, bc.Label] = {}
        self._ref_labels: t.Dict[str, bc.Label] = {}
        self._tb: t.Optional[bc.TryBegin] = None
        self._instrs = bc.Bytecode()
        self._instrs.name = name or "<assembly>"
        self._instrs.filename = filename or __file__
        self._lineno = lineno
        self._bind_opargs: t.Dict[int, BindOpArg] = {}
        self._codes: t.Dict[str, Assembly] = {}
        self._code_refs: t.Dict[int, CodeRefOpArg] = {}

    def _parse_ident(self, text: str) -> str:
        if not text.isidentifier():
            raise ValueError("invalid identifier %s" % text)

        return text

    def _parse_number(self, text: str) -> t.Optional[int]:
        try:
            return int(text)
        except ValueError:
            return None

    def _parse_label(self, line: str) -> t.Optional[bc.Label]:
        if not line.endswith(":"):
            return None

        label_ident = self._parse_ident(line[:-1])
        if label_ident in self._labels:
            raise ValueError("label %s already defined" % label_ident)

        label = self._labels[label_ident] = self._ref_labels.pop(label_ident, None) or bc.Label()

        return label

    def _parse_label_ref(self, text: str) -> t.Optional[bc.Label]:
        if not text.startswith("@"):
            return None

        label_ident = self._parse_ident(text[1:])

        try:
            return self._labels[label_ident]
        except KeyError:
            try:
                return self._ref_labels[label_ident]
            except KeyError:
                label = self._ref_labels[label_ident] = bc.Label()
                return label

    def _parse_string_ref(self, text: str) -> t.Optional[str]:
        if not text.startswith("$"):
            return None

        return text[1:]

    def _parse_try_begin(self, line: str) -> t.Optional[bc.TryBegin]:
        try:
            head, label_ref, *lasti = line.split(maxsplit=2)
        except ValueError:
            return None

        if head != "try":
            return None

        if self._tb is not None:
            msg = "cannot start try block while another is open"
            raise ValueError(msg)

        label = self._parse_label_ref(label_ref)
        if label is None:
            msg = "invalid label reference for try block"
            raise ValueError(msg)

        tb = self._tb = bc.TryBegin(label, push_lasti=bool(lasti))

        return tb

    def _parse_try_end(self, line: str) -> t.Optional[bc.TryEnd]:
        if line != "tried":
            return None

        if self._tb is None:
            msg = "cannot end try block while none is open"
            raise ValueError(msg)

        end = bc.TryEnd(self._tb)

        self._tb = None

        return end

    def _parse_opcode(self, text: str) -> str:
        opcode = text.upper()
        if opcode not in dis.opmap:
            raise ValueError("unknown opcode %s" % opcode)

        return opcode

    def _parse_expr(self, text: str) -> t.Any:
        frame = sys._getframe(1)
        _globals = frame.f_globals.copy()
        _globals["asm"] = bc
        return eval(text, _globals, frame.f_locals)  # noqa: S307

    def _parse_opcode_arg(self, text: str) -> t.Union[bc.Label, str, int, t.Any]:
        return (
            self._parse_label_ref(text)
            or self._parse_string_ref(text)
            or self._parse_number(text)
            or self._parse_expr(text)
        )

    def _parse_bind_opcode_arg(self, text: str) -> t.Optional[str]:
        if not text.startswith("{") or not text.endswith("}"):
            return None

        return text[1:-1]

    def _parse_code_ref_arg(self, text: str) -> t.Optional[str]:
        if not text.startswith("."):
            return None

        return text[1:]

    def _parse_instruction(self, line: str) -> t.Optional[t.Union[bc.Instr, BindOpArg]]:
        opcode, *args = line.split(maxsplit=1)

        if args:
            (arg,) = args

            bind_arg = self._parse_bind_opcode_arg(arg)
            if bind_arg is not None:
                entry = BindOpArg(self._parse_opcode(opcode), bind_arg, lineno=self._lineno)

                # TODO: What happens if a bind arg occurs multiple times?
                self._bind_opargs[len(self._instrs)] = entry

                return entry

            code_ref = self._parse_code_ref_arg(arg)
            if code_ref is not None:
                entry = CodeRefOpArg(self._parse_opcode(opcode), code_ref, lineno=self._lineno)

                self._code_refs[len(self._instrs)] = entry

                return entry

        return bc.Instr(
            *transform_instruction(self._parse_opcode(opcode), *map(self._parse_opcode_arg, args)),
            lineno=self._lineno,
        )

    def _parse_code_begin(self, line: str) -> t.Optional[CodeBegin]:
        try:
            if not line.endswith(")"):
                return None
            line = line[:-1]

            head, details = line.split(maxsplit=1)
            if head != "code":
                return None
        except ValueError:
            return None

        try:
            name, _, arglist = details.partition("(")

            name = name.strip()
            arglist = arglist.strip()
            args = [arg.strip() for arg in arglist.split(",")]
        except Exception as e:
            msg = f"invalid code block header: {e}"
            raise ValueError(msg) from e

        return CodeBegin(name, args)

    def _parse_code_end(self, line: str) -> t.Optional[CodeEnd]:
        return CodeEnd() if line == "end" else None

    def _parse_line(self, line: str) -> t.Union[bc.Instr, bc.Label, bc.TryBegin, bc.TryEnd]:
        entry = (
            self._parse_label(line)
            or self._parse_try_begin(line)
            or self._parse_try_end(line)
            or self._parse_code_begin(line)
            or self._parse_code_end(line)
            or self._parse_instruction(line)
        )

        if entry is None:
            raise ValueError("invalid line %s" % line)

        return entry

    def _validate(self) -> None:
        if self._ref_labels:
            raise ValueError("undefined labels: %s" % ", ".join(self._ref_labels))

    def _parse_code(self, lines: t.Iterable[t.Tuple[int, str]]) -> None:
        for n, line in lines:
            try:
                entry = self._parse_line(line)
            except Exception as e:
                raise SpasmParseError(self._instrs.filename, n) from e

            if isinstance(entry, CodeEnd):
                break

            if isinstance(entry, (bc.Instr, BaseOpArg)):
                entry.lineno = n

            self._instrs.append(entry)

        else:
            msg = f"code block {self._instrs.name} not terminated"
            raise ValueError(msg)

        self._validate()

    def _parse(self, lines: t.Iterable[t.Tuple[int, str]]) -> None:
        for n, line in lines:
            try:
                entry = self._parse_line(line)
            except Exception as e:
                raise SpasmParseError(self._instrs.filename, n) from e

            if isinstance(entry, CodeBegin):
                code = self._codes[entry.name] = Assembly(
                    name=entry.name, filename=self._instrs.filename, lineno=self._lineno
                )
                code._parse_code(lines)

                code._instrs.argnames = entry.args or None
                code._instrs.argcount = len(code._instrs.argnames or [])
                # TODO: Add support for other types of arguments

                continue

            if isinstance(entry, CodeEnd):
                msg = "code end outside of code block"
                raise ValueError(msg)

            if isinstance(entry, (bc.Instr, BaseOpArg)):
                entry.lineno = n

            self._instrs.append(entry)

        self._validate()

    def parse(self, text: str) -> None:
        self._parse(
            (n, _)
            for n, _ in ((n, _.strip()) for n, _ in enumerate(text.splitlines(), start=1))
            if _ and not _.startswith("#")
        )

    def bind(self, args: t.Optional[t.Dict[str, t.Any]] = None, lineno: t.Optional[int] = None) -> bc.Bytecode:
        if not self._bind_opargs and not self._code_refs:
            if lineno is not None:
                return relocate(self._instrs, lineno)
            return self._instrs

        missing_bind_args = {_.arg for _ in self._bind_opargs.values()} - set(args or {})
        if missing_bind_args:
            raise ValueError("missing bind args: %s" % ", ".join(missing_bind_args))

        # If we have bind opargs, the bytecode we parsed has some
        # BindOpArg placeholders that need to be resolved. Therefore, we
        # make a copy of the parsed bytecode and replace the BindOpArg
        # placeholders with the resolved values.
        instrs = bc.Bytecode(self._instrs)
        for i, arg in self._bind_opargs.items():
            instrs[i] = arg(t.cast(dict, args), lineno=lineno)

        if self._code_refs:
            codes = {name: code.compile(args, lineno) for name, code in self._codes.items()}
            for i, arg in self._code_refs.items():
                instrs[i] = arg(codes, lineno=lineno)

        return relocate(instrs, lineno) if lineno is not None else instrs

    def compile(  # noqa: A003
        self,
        bind_args: t.Optional[t.Dict[str, t.Any]] = None,
        lineno: t.Optional[int] = None,
    ) -> CodeType:
        return self.bind(bind_args, lineno=lineno).to_code()

    def _label_ident(self, label: bc.Label) -> str:
        return next(ident for ident, _ in self._labels.items() if _ is label)

    def dis(self) -> None:
        for entry in self._instrs:
            if isinstance(entry, bc.Instr):
                print(f"    {entry.name:<32}{entry.arg if entry.arg is not None else ''}")  # noqa: T201
            elif isinstance(entry, BindOpArg):
                print(f"    {entry.name:<32}{{{entry.arg}}}")  # noqa: T201
            elif isinstance(entry, bc.Label):
                print(f"{self._label_ident(entry)}:")  # noqa: T201
            elif isinstance(entry, bc.TryBegin):
                print(f"try @{self._label_ident(entry.target)} (lasti={entry.push_lasti})")  # noqa: T201

    def __iter__(self) -> t.Iterator[bc.Instr]:
        return iter(self._instrs)
