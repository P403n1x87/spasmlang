import importlib
import time
from argparse import ArgumentParser
from pathlib import Path
from types import CodeType

from spasm._version import __version__  # type: ignore[import]
from spasm.asm import Assembly


class SpasmError(Exception):
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
        raise SpasmError(msg) from e
    except Exception as e:
        msg = "Cannot create pyc file"
        raise SpasmError(msg) from e

    with file.open("wb") as stream:
        stream.write(data)
        stream.flush()


def assemble(sourcefile: Path) -> CodeType:
    asm = Assembly(name="<module>", filename=str(sourcefile.resolve()), lineno=1)
    asm.parse(sourcefile.read_text())
    return asm.compile()


def main() -> None:
    argp = ArgumentParser()

    argp.add_argument("file", type=Path)
    argp.add_argument("-V", "--version", action="version", version=__version__)

    args = argp.parse_args()

    try:
        dump_code_to_file(
            assemble(args.file),
            args.file.with_suffix(".pyc"),
        )
    except Exception as e:
        print("Spasm error: ", str(e))  # noqa: T201


if __name__ == "__main__":
    main()
