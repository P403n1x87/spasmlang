import sys

import pytest

from spasm import Assembly


def test_assembly_bind_args():
    asm = Assembly()

    asm.parse(
        r"""
        load_const      {retval}
        return_value
        """
    )

    assert eval(asm.compile({"retval": 42})) == 42  # noqa: S307


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="CPython 3.11 bytecode only")
def test_assembly_exception_table():
    asm = Assembly()

    asm.parse(
        r"""
            resume                      0

        try @exception
            load_const                  {answer}
            load_const                  42
            compare_op                  asm.Compare.NE
            pop_jump_forward_if_false   @correct_answer
            load_const                  Exception("Not the answer")
            raise_varargs               1
        tried

        correct_answer:
            load_const                  None
            return_value

        exception:
            push_exc_info
            return_value
        """
    )

    assert eval(asm.compile({"answer": 42})) is None  # noqa: S307
    assert isinstance(eval(asm.compile({"answer": 41})), Exception)  # noqa: S307


@pytest.mark.skipif(sys.version_info[:2] != (3, 11), reason="CPython 3.11 bytecode only")
def test_assembly_sub_code():
    asm = Assembly()

    asm.parse(
        r"""
        code greet(who)
            resume                      0
            load_global                 (True, "print")
            load_const                  "Hello, "
            load_fast                   $who
            format_value                0
            build_string                2
            precall                     1
            call                        1
            pop_top
            load_fast                   $who
            return_value
        end

            resume 0
            load_const                  .greet
            make_function               0
            store_name                  $greet
            load_const                  None
            return_value
        """
    )

    _globals = {}
    exec(asm.compile(), _globals)  # noqa: S102

    assert _globals["greet"]("World") == "World"


@pytest.mark.skipif(sys.version_info[:2] < (3, 12), reason="CPython 3.12+ bytecode only")
def test_assembly_load_attr_transformation():
    asm = Assembly()

    asm.parse(
        r"""
            resume                      0
            load_const                  {arg}
            load_method                 $foo
            load_const                  {arg}
            load_attr                   $foo
            build_tuple                 3
            return_value
        """
    )

    class Foo:
        def foo(self):
            pass

    f = Foo()

    assert eval(asm.compile({"arg": f})) == (Foo.foo, f, f.foo)  # noqa: S307
