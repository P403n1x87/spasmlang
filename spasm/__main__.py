import importlib
import marshal
import sys
import time
from argparse import ArgumentParser
from pathlib import Path
from types import CodeType

import bytecode as bc  # type: ignore[import]

from spasm._version import __version__  # type: ignore[import]
from spasm.asm import Assembly


class SpasmError(Exception):
    pass


class SpasmUnmarshalError(SpasmError):
    pass


def dump_code_to_file(code: CodeType, file: Path) -> None:
    try:
        data = importlib._bootstrap_external._code_to_timestamp_pyc(  # type: ignore[attr-defined]
            code,
            time.time(),
            len(code.co_code),
        )
    except ValueError as e:
        msg = "Cannot unmarshal code object"
        raise SpasmUnmarshalError(msg) from e
    except Exception as e:
        msg = "Cannot assemble source to pyc file"
        raise SpasmError(msg) from e

    with file.open("wb") as stream:
        stream.write(data)
        stream.flush()


def assemble(sourcefile: Path) -> CodeType:
    asm = Assembly(name="<module>", filename=str(sourcefile.resolve()), lineno=1)
    asm.parse(sourcefile.read_text())
    return asm.compile()


def find_unmarshallable_objects(asm: Assembly) -> None:
    for instr in asm._instrs:
        if not isinstance(instr, bc.Instr):
            continue
        if instr.arg not in (None, bc.UNSET):
            try:
                marshal.dumps(instr.arg)
            except Exception:
                print(  # noqa: T201
                    f"  in {asm._instrs.filename}, line {instr.lineno}: object "
                    f"of type {type(instr.arg)} cannot be unmarshalled"
                )

    for code in asm._codes.values():
        find_unmarshallable_objects(code)


def spasm(sourcefile: Path) -> None:
    try:
        asm = Assembly(name="<module>", filename=str(sourcefile.resolve()), lineno=1)

        asm.parse(sourcefile.read_text())

        code = asm.compile()

        dump_code_to_file(code, sourcefile.with_suffix(".pyc"))

    except Exception as e:
        print("Spasm error:", str(e))  # noqa: T201
        if isinstance(e, SpasmUnmarshalError):
            find_unmarshallable_objects(asm)
        raise


def main() -> None:
    argp = ArgumentParser()

    argp.add_argument("file", type=Path)
    argp.add_argument("-V", "--version", action="version", version=__version__)

    args = argp.parse_args()

    try:
        spasm(args.file)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
