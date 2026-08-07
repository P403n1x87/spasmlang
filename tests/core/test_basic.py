"""Smoke tests: round-trip a code object through from_code -> to_code."""

import sys
import types

from spasm import _core

Bytecode = _core.Bytecode


def make_code(fn):
    return fn.__code__


def test_version_hex():
    # PY_VERSION_HEX encodes major.minor.micro; check only major.minor.
    assert (_core.PY_VERSION_HEX >> 16) == (sys.version_info.major << 8 | sys.version_info.minor)


def test_round_trip_simple():
    def f(x):
        return x + 1

    bc = Bytecode.from_code(make_code(f))
    new_code = bc.to_code()
    assert isinstance(new_code, types.CodeType)

    new_f = types.FunctionType(new_code, f.__globals__)
    assert new_f(41) == 42


def test_round_trip_loop():
    def total(n):
        s = 0
        for i in range(n):
            s += i
        return s

    bc = Bytecode.from_code(make_code(total))
    new_code = bc.to_code()
    new_fn = types.FunctionType(new_code, total.__globals__)
    assert new_fn(10) == 45


def test_round_trip_try_except():
    def safe_div(a, b):
        try:
            return a // b
        except ZeroDivisionError:
            return None

    bc = Bytecode.from_code(make_code(safe_div))
    new_code = bc.to_code()
    new_fn = types.FunctionType(new_code, safe_div.__globals__)
    assert new_fn(10, 2) == 5
    assert new_fn(10, 0) is None


if __name__ == "__main__":
    test_version_hex()
    test_round_trip_simple()
    test_round_trip_loop()
    test_round_trip_try_except()
    print(f"All tests passed (Python {sys.version})")
