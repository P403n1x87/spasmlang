"""Marshalling a code object out to a bare ``.pyc`` file.

Shared by the ``spasm`` CLI (:mod:`spasm.__main__`) and the build backend
(:mod:`spasm.buildbackend`) — both need to turn a compiled/assembled
:class:`types.CodeType` into the same on-disk artifact.
"""

import importlib
import time
from pathlib import Path
from types import CodeType


class PycWriteError(Exception):
    pass


class PycUnmarshalError(PycWriteError):
    pass


def code_to_pyc_bytes(code: CodeType) -> bytes:
    """Marshal ``code`` into the bytes of a timestamp-based ``.pyc`` file."""
    try:
        return importlib._bootstrap_external._code_to_timestamp_pyc(  # type: ignore[attr-defined]
            code,
            time.time(),
            len(code.co_code),
        )
    except ValueError as e:
        msg = "cannot unmarshal code object"
        raise PycUnmarshalError(msg) from e
    except Exception as e:
        msg = "cannot marshal code object to pyc"
        raise PycWriteError(msg) from e


def write_pyc(code: CodeType, file: Path) -> None:
    """Marshal ``code`` and write it out to ``file``."""
    data = code_to_pyc_bytes(code)
    with file.open("wb") as stream:
        stream.write(data)
        stream.flush()
