import sys

import pytest

import spasm

PY = sys.version_info[:2]


def test_asm_bind_args():
    @spasm.asm(retval=42)
    def answer():
        """
        load_const {retval}
        return_value
        """

    assert answer() == 42


def test_asm_uses_real_signature_for_arguments():
    if PY >= (3, 11):

        @spasm.asm()
        def add(x, y):
            """
            resume 0
            load_fast $x
            load_fast $y
            binary_op 0
            return_value
            """
    else:

        @spasm.asm()
        def add(x, y):
            """
            load_fast $x
            load_fast $y
            binary_add
            return_value
            """

    assert add(3, 4) == 7


def test_asm_clears_docstring():
    @spasm.asm(retval=1)
    def one():
        """
        load_const {retval}
        return_value
        """

    assert one.__doc__ is None


def test_asm_requires_docstring():
    with pytest.raises(ValueError, match="docstring"):

        @spasm.asm()
        def no_doc():
            pass


def test_asm_rejects_varargs():
    with pytest.raises(ValueError, match=r"\*args"):

        @spasm.asm()
        def varargs(*args):
            """
            load_const None
            return_value
            """


def test_asm_rejects_kwonly():
    with pytest.raises(ValueError, match="keyword-only"):

        @spasm.asm()
        def kwonly(*, x):
            """
            load_const None
            return_value
            """
