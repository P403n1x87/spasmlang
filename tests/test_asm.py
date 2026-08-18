import dis
import sys

import pytest

from spasm import Assembly
from spasm._asm import SpasmParseError
from spasm.bytecode import CO_NESTED

PY = sys.version_info[:2]

# Opcode spellings that moved between the supported versions. Keeping these in
# one place lets the tests below cover every version rather than just the one
# they were originally written against.
RESUME = "resume 0" if PY >= (3, 11) else ""
POP_JUMP_IF_FALSE = "pop_jump_forward_if_false" if PY == (3, 11) else "pop_jump_if_false"
if PY >= (3, 12):
    LOAD_PRINT, CALL = 'load_global (True, "print")', "call 1"
elif PY == (3, 11):
    LOAD_PRINT, CALL = 'load_global (True, "print")', "precall 1\n    call 1"
else:
    LOAD_PRINT, CALL = "load_global $print", "call_function 1"
# Before 3.11, MAKE_FUNCTION also wants the qualname on the stack.
QUALNAME = "" if PY >= (3, 11) else 'load_const "greet"'
# 3.11 replaced the per-operator binary opcodes with a single BINARY_OP, made
# the stack manipulation opcodes take an argument, and turned the unconditional
# backward jump into a relative one.
BINARY_ADD = "binary_op 0" if PY >= (3, 11) else "binary_add"
COPY = "copy 1" if PY >= (3, 11) else "dup_top"
JUMP_BACKWARD = "jump_backward" if PY >= (3, 11) else "jump_absolute"


def test_assembly_bind_args():
    asm = Assembly()

    asm.parse(
        r"""
        load_const      {retval}
        return_value
        """
    )

    assert eval(asm.compile({"retval": 42})) == 42  # noqa: S307


@pytest.mark.skipif(PY < (3, 11), reason="no exception table before CPython 3.11")
def test_assembly_exception_table():
    asm = Assembly()

    asm.parse(
        rf"""
            resume                      0

        try @exception
            load_const                  {{answer}}
            load_const                  42
            compare_op                  asm.Compare.NE
            {POP_JUMP_IF_FALSE}         @correct_answer
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


def test_assembly_sub_code(capfd):
    asm = Assembly()

    asm.parse(
        rf"""
        code greet(who)
            {RESUME}
            {LOAD_PRINT}
            load_fast                   $who
            {CALL}
            pop_top
            load_fast                   $who
            return_value
        end

            {RESUME}
            load_const                  .greet
            {QUALNAME}
            make_function               0
            store_name                  $greet
            load_const                  None
            return_value
        """
    )

    _globals = {}
    exec(asm.compile(), _globals)  # noqa: S102

    assert _globals["greet"]("World") == "World"
    assert capfd.readouterr().out == "World\n"


@pytest.mark.skipif(PY < (3, 12), reason="LOAD_METHOD is only rewritten from CPython 3.12")
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


# ---------------------------------------------------------------------------
# Labels
#
# Labels are positions into the instruction list rather than entries in it, so
# what needs covering is the position arithmetic: references in both
# directions, several labels landing on one instruction, and a position past
# the last instruction.
# ---------------------------------------------------------------------------


def test_assembly_backward_jump():
    """A label can be referenced from after its definition as well as before."""
    asm = Assembly()

    asm.parse(
        rf"""
            {RESUME}
            load_const                  0
        loop:
            load_const                  1
            {BINARY_ADD}
            {COPY}
            load_const                  3
            compare_op                  asm.Compare.LT
            {POP_JUMP_IF_FALSE}         @done
            {JUMP_BACKWARD}             @loop
        done:
            return_value
        """
    )

    assert eval(asm.compile()) == 3  # noqa: S307


def test_assembly_aliased_labels():
    """Several labels can share a position, and all resolve to it."""
    asm = Assembly()

    asm.parse(
        rf"""
            {RESUME}
            load_const                  {{flag}}
            {POP_JUMP_IF_FALSE}         @from_branch
            jump_forward                @from_jump
        from_branch:
        from_jump:
            load_const                  "hit"
            return_value
        """
    )

    assert eval(asm.compile({"flag": True})) == "hit"  # noqa: S307
    assert eval(asm.compile({"flag": False})) == "hit"  # noqa: S307


@pytest.mark.skipif(PY < (3, 11), reason="no exception table before CPython 3.11")
def test_assembly_protected_region_to_end_of_code():
    """A ``tried`` on the last line bounds the region past the last instruction.

    The handler has to come first for this to be reachable, which also means
    the region's end label has no instruction to attach to.
    """
    asm = Assembly()

    asm.parse(
        rf"""
            {RESUME}
            jump_forward                @start
        handler:
            push_exc_info
            return_value
        start:
        try @handler
            load_const                  Exception("boom")
            raise_varargs               1
        tried
        """
    )

    assert str(eval(asm.compile())) == "boom"  # noqa: S307


# ---------------------------------------------------------------------------
# Exception table
# ---------------------------------------------------------------------------


@pytest.mark.skipif(PY < (3, 11), reason="no exception table before CPython 3.11")
def test_assembly_sequential_try_blocks():
    """Two regions in a row stay distinct and route to their own handlers."""
    from spasm._core import Bytecode

    asm = Assembly()

    asm.parse(
        rf"""
            {RESUME}
        try @first_handler
            nop
        tried
            jump_forward                @second
        first_handler:
            push_exc_info
            return_value
        second:
        try @second_handler
            load_const                  {{raised}}
            raise_varargs               1
        tried
        second_handler:
            push_exc_info
            return_value
        """
    )

    code = asm.compile({"raised": KeyError("two")})
    assert len(Bytecode.from_code(code).exc_entries) == 2
    # The first region doesn't raise, so control reaches the second one, whose
    # handler is the one that must run.
    assert isinstance(eval(code), KeyError)  # noqa: S307


@pytest.mark.skipif(PY < (3, 11), reason="no exception table before CPython 3.11")
def test_assembly_protected_region_reachable_only_by_unwinding():
    """A region entered from a handler can't have its depth inferred.

    CPython records the base depth of the enclosing try block, which it reads
    off pseudo-instructions that assembled bytecode doesn't have. Rather than
    guess, the core refuses.
    """
    asm = Assembly()

    asm.parse(
        rf"""
            {RESUME}
        try @first_handler
            load_const                  Exception("boom")
            raise_varargs               1
        tried
        first_handler:
        try @second_handler
            raise_varargs               0
        tried
        second_handler:
            push_exc_info
            return_value
        """
    )

    with pytest.raises(ValueError, match="only reachable by unwinding"):
        asm.compile()


@pytest.mark.skipif(PY < (3, 11), reason="no exception table before CPython 3.11")
def test_assembly_try_lasti():
    """The ``lasti`` marker reaches the compiled exception table."""
    from spasm._core import Bytecode

    def source(lasti):
        asm = Assembly()
        asm.parse(
            rf"""
                {RESUME}
            try @handler {lasti}
                load_const              Exception("boom")
                raise_varargs           1
            tried
            handler:
                push_exc_info
                return_value
            """
        )
        return asm.compile()

    assert [e.lasti for e in Bytecode.from_code(source("lasti")).exc_entries] == [True]
    assert [e.lasti for e in Bytecode.from_code(source("")).exc_entries] == [False]


# ---------------------------------------------------------------------------
# Bind arguments and line numbers
# ---------------------------------------------------------------------------


def test_assembly_bind_arg_used_twice():
    """The same bind argument can appear at more than one position."""
    asm = Assembly()

    asm.parse(
        r"""
        load_const      {value}
        load_const      {value}
        build_tuple     2
        return_value
        """
    )

    assert eval(asm.compile({"value": 42})) == (42, 42)  # noqa: S307


def test_assembly_missing_bind_args():
    asm = Assembly()
    asm.parse(
        r"""
        load_const      {a}
        load_const      {b}
        build_tuple     2
        return_value
        """
    )

    with pytest.raises(ValueError, match="missing bind args: a, b"):
        asm.compile()


def test_assembly_lineno_override():
    """``lineno`` stamps the whole assembly, replacing the source positions."""
    asm = Assembly()
    asm.parse(
        rf"""
        {RESUME}
        load_const      None
        return_value
        """
    )

    default = asm.compile()
    # Line 2 is the RESUME, or blank and skipped on the versions without one.
    assert {ln for _, ln in dis.findlinestarts(default)} == ({2, 3, 4} if RESUME else {3, 4})

    stamped = asm.compile(lineno=100)
    assert stamped.co_firstlineno == 100
    assert {ln for _, ln in dis.findlinestarts(stamped)} == {100}


def test_assembly_comments_and_blank_lines():
    asm = Assembly()
    asm.parse(
        rf"""
        # A leading comment.

        {RESUME}

        # An interleaved one.
        load_const      "value"
        return_value
        """
    )

    assert eval(asm.compile()) == "value"  # noqa: S307


# ---------------------------------------------------------------------------
# Invalid source
# ---------------------------------------------------------------------------


def test_assembly_unknown_opcode():
    asm = Assembly(filename="test.spasm")

    with pytest.raises(SpasmParseError) as exc_info:
        asm.parse(
            r"""
            load_const      1
            no_such_opcode
            """
        )

    assert exc_info.value.filename == "test.spasm"
    assert exc_info.value.lineno == 3
    assert "unknown opcode NO_SUCH_OPCODE" in str(exc_info.value)


def test_assembly_duplicate_label():
    asm = Assembly()

    with pytest.raises(SpasmParseError, match="label here already defined"):
        asm.parse(
            r"""
            here:
            nop
            here:
            nop
            """
        )


def test_assembly_undefined_label():
    asm = Assembly()

    with pytest.raises(ValueError, match="undefined labels: nowhere"):
        asm.parse(
            r"""
            jump_forward    @nowhere
            """
        )


@pytest.mark.skipif(PY < (3, 11), reason="try blocks require an exception table")
@pytest.mark.parametrize(
    "source,message",
    [
        ("try @handler\nnop", "unterminated try block"),
        ("nop\ntried", "cannot end try block while none is open"),
        ("try @handler\ntry @handler\nnop\ntried", "cannot start try block while another is open"),
    ],
)
def test_assembly_malformed_try_blocks(source, message):
    asm = Assembly()

    # Line-level errors are wrapped in a SpasmParseError to carry the position;
    # the ones the end-of-parse validation raises come through as they are.
    with pytest.raises((ValueError, SpasmParseError), match=message):
        asm.parse(source + "\nhandler:\nnop\n")


@pytest.mark.skipif(PY >= (3, 11), reason="try blocks are supported from CPython 3.11")
def test_assembly_try_block_rejected_before_311():
    asm = Assembly()

    with pytest.raises(SpasmParseError, match=r"try blocks require Python 3\.11 or later"):
        asm.parse("try @handler\nnop\ntried\nhandler:\nnop\n")


def test_assembly_unterminated_code_block():
    asm = Assembly()

    with pytest.raises(ValueError, match="code block greet not terminated"):
        asm.parse(
            r"""
            code greet(who)
                load_fast   $who
                return_value
            """
        )


def test_assembly_code_end_outside_block():
    asm = Assembly()

    with pytest.raises(ValueError, match="code end outside of code block"):
        asm.parse("nop\nend\n")


def test_assembly_nested_code_blocks():
    """A block declared inside another is a constant of the enclosing one."""
    asm = Assembly()

    asm.parse(
        rf"""
        code outer()
            {RESUME}
            code inner()
                {RESUME}
                load_const  "from inner"
                return_value
            end
            load_const  .inner
            {"" if PY >= (3, 11) else 'load_const "inner"'}
            make_function 0
            return_value
        end

            {RESUME}
            load_const  .outer
            {"" if PY >= (3, 11) else 'load_const "outer"'}
            make_function 0
            store_name  $outer
            load_const  None
            return_value
        """
    )

    _globals = {}
    exec(asm.compile(), _globals)  # noqa: S102

    # inner is not visible at module level: it belongs to outer's constants.
    assert "inner" not in asm._codes
    assert _globals["outer"]()() == "from inner"
    # Nested inside a function body, so lexically nested; outer itself is not.
    assert _globals["outer"].__code__.co_flags & CO_NESTED == 0
    assert _globals["outer"]().__code__.co_flags & CO_NESTED


def test_assembly_duplicate_code_block():
    asm = Assembly()

    with pytest.raises(ValueError, match="duplicate code block f"):
        asm.parse(
            r"""
            code f()
                return_value
            end
            code f()
                return_value
            end
            """
        )


def test_assembly_malformed_code_block_header():
    """A bad header is reported as such, not as an unknown opcode."""
    asm = Assembly()

    with pytest.raises(SpasmParseError, match="invalid code block header"):
        asm.parse("code f(\nend\n")


def test_assembly_closure():
    """A cell in the enclosing block, a free variable in the nested one.

    ``n`` is both a parameter of ``outer`` and a cell it hands to ``inner``, so
    it appears in the argument list and in the cell list; ``inner`` declares it
    free. Neither can be inferred from the instruction stream, which is why the
    header spells them out.
    """
    asm = Assembly()

    # LOAD_CLOSURE became a pseudo-opcode in 3.13 — from there a cell is loaded
    # with LOAD_FAST, since the oparg indexes localsplus either way.
    load_cell = "load_fast" if PY >= (3, 13) else "load_closure"
    if PY >= (3, 13):
        make_closure = "make_function\n    set_function_attribute 8"
    elif PY >= (3, 11):
        make_closure = "make_function 8"
    else:
        make_closure = 'load_const "inner"\n    make_function 8'

    asm.parse(
        rf"""
        code outer(n)[n]
            {"make_cell $n" if PY >= (3, 11) else ""}
            {RESUME}
            code inner()<n>
                {"copy_free_vars 1" if PY >= (3, 11) else ""}
                {RESUME}
                load_deref  $n
                return_value
            end
            {load_cell}    $n
            build_tuple 1
            load_const  .inner
            {make_closure}
            return_value
        end

            {RESUME}
            load_const  .outer
            {"" if PY >= (3, 11) else 'load_const "outer"'}
            make_function 0
            store_name  $outer
            load_const  None
            return_value
        """
    )

    _globals = {}
    exec(asm.compile(), _globals)  # noqa: S102

    outer = _globals["outer"]
    assert outer.__code__.co_cellvars == ("n",)
    # The parameter and the cell share one localsplus slot, as they do in code
    # the compiler produces.
    assert outer.__code__.co_varnames == ("n",)

    inner = outer(42)
    assert inner.__code__.co_freevars == ("n",)
    assert inner() == 42
    assert outer(7)() == 7


def test_assembly_cell_and_free_conflict():
    asm = Assembly()

    with pytest.raises(ValueError, match="declared both cell and free: n"):
        asm.parse(
            r"""
            code f()[n]<n>
                return_value
            end
            """
        )


def test_assembly_free_variable_shadows_argument():
    asm = Assembly()

    with pytest.raises(ValueError, match="free variables shadow arguments: n"):
        asm.parse(
            r"""
            code f(n)<n>
                return_value
            end
            """
        )


# ---------------------------------------------------------------------------
# Large operands
#
# An oparg wider than a byte needs EXTENDED_ARG prefix words, which change the
# size of the instruction, which moves every jump target after it — so the
# assembler grows the prefixes to a fixed point. Padding is swept across the
# 256-code-unit boundary rather than picked once, since an off-by-one in that
# loop only shows up at the exact width where a prefix first becomes necessary.
# ---------------------------------------------------------------------------

# Distances are in code units, and the inline caches attached to an instruction
# count towards them, so the padding needed to cross the boundary is not the
# same on every version. Sweeping a range wide enough to contain it either way
# avoids having to model that here.
PADDINGS = list(range(120, 136)) + list(range(248, 264))


@pytest.mark.parametrize("padding", PADDINGS)
def test_assembly_extended_arg_forward_jump(padding):
    """A forward jump over enough code needs EXTENDED_ARG and still lands."""
    asm = Assembly()

    nops = "\n".join(["            nop"] * padding)
    asm.parse(
        rf"""
            {RESUME}
            load_const                  True
            {POP_JUMP_IF_FALSE}         @wrong
            jump_forward                @right
{nops}
        wrong:
            load_const                  "wrong"
            return_value
        right:
            load_const                  "right"
            return_value
        """
    )

    assert eval(asm.compile()) == "right"  # noqa: S307


@pytest.mark.parametrize("padding", PADDINGS)
def test_assembly_extended_arg_backward_jump(padding):
    """Same for a backward jump, whose distance grows as prefixes are added.

    Growing a backward jump's own prefix pushes the code it jumps over further
    away, so the fixed point takes an extra round here where the forward case
    settles immediately.
    """
    asm = Assembly()

    nops = "\n".join(["            nop"] * padding)
    asm.parse(
        rf"""
            {RESUME}
            load_const                  0
        loop:
            load_const                  1
            {BINARY_ADD}
{nops}
            {COPY}
            load_const                  3
            compare_op                  asm.Compare.LT
            {POP_JUMP_IF_FALSE}         @done
            {JUMP_BACKWARD}             @loop
        done:
            return_value
        """
    )

    assert eval(asm.compile()) == 3  # noqa: S307


@pytest.mark.parametrize("count", [255, 256, 257, 300])
def test_assembly_extended_arg_const_index(count):
    """A constant past index 255 is reached through an EXTENDED_ARG prefix."""
    asm = Assembly()

    # Distinct constants so none of them can be folded into an earlier slot;
    # each is loaded and dropped, leaving the last one as the return value.
    fill = "\n".join(f"            load_const  {i}\n            pop_top" for i in range(count))
    asm.parse(
        rf"""
            {RESUME}
{fill}
            load_const                  "last"
            return_value
        """
    )

    code = asm.compile()
    assert len(code.co_consts) > count
    # Index 255 is still a single byte, so the smallest case is the control:
    # it must come out without a prefix.
    extended = any(i.opname == "EXTENDED_ARG" for i in dis.get_instructions(code))
    assert extended is (count > 255)
    assert eval(code) == "last"  # noqa: S307


@pytest.mark.skipif(PY < (3, 11), reason="no exception table before CPython 3.11")
@pytest.mark.parametrize("padding", PADDINGS)
def test_assembly_extended_arg_protected_region(padding):
    """Exception table offsets track the prefixes too, not just jumps.

    The table is encoded separately from the bytecode, from the same labels, so
    it is possible to get the jumps right and the handler offset wrong.
    """
    asm = Assembly()

    nops = "\n".join(["            nop"] * padding)
    asm.parse(
        rf"""
            {RESUME}
            jump_forward                @start
        handler:
            push_exc_info
            return_value
        start:
        try @handler
{nops}
            load_const                  Exception("boom")
            raise_varargs               1
        tried
            load_const                  "not reached"
            return_value
        """
    )

    assert str(eval(asm.compile())) == "boom"  # noqa: S307


# ---------------------------------------------------------------------------
# Disassembly
# ---------------------------------------------------------------------------


@pytest.mark.skipif(PY < (3, 11), reason="no exception table before CPython 3.11")
def test_assembly_dis(capfd):
    """``dis`` prints the directives back at the positions they were parsed at."""
    asm = Assembly()

    asm.parse(
        r"""
        try @handler
        body:
            nop
            load_const  {value}
            load_const  .missing
        tried
        handler:
            push_exc_info
            return_value
        """
    )

    asm.dis()

    assert capfd.readouterr().out.splitlines() == [
        "body:",
        "try @handler (lasti=False)",
        "    NOP                             ",
        "    LOAD_CONST                      {value}",
        "    LOAD_CONST                      .missing",
        "tried",
        "handler:",
        "    PUSH_EXC_INFO                   ",
        "    RETURN_VALUE                    ",
    ]
