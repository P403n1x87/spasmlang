import dis

import spasm

# Callees have to live at module scope: a callee defined inside a test
# function would be a closure over that function (LOAD_DEREF), not a global
# (LOAD_GLOBAL) — and the inliner only handles LOAD_GLOBAL-resolved calls.


def add_one(x):
    return x + 1


def add(a, b):
    return a + b


def double(x):
    return x * 2


def sign(x):
    if x > 0:
        return 1
    return -1


def add_kwarg(a, b=1):
    return a + b


def total(*args):
    return sum(args)


def gen(x):
    yield x


def bump(x):
    x = x + 1
    return x * 2


def double_use(a, b):
    return a + a + b


def _has_call(func: object) -> bool:
    return any(instr.opname.startswith("CALL") or instr.opname == "PRECALL" for instr in dis.get_instructions(func))


def test_inline_single_arg_call() -> None:
    @spasm.inline
    def compute(n):
        return add_one(n) * 2

    assert [compute(n) for n in (0, 1, -3, 10)] == [2, 4, -4, 22]
    assert not _has_call(compute)


def test_inline_two_arg_call() -> None:
    @spasm.inline
    def compute(x, y):
        return add(x, y) + add(y, x)

    assert compute(3, 4) == 14
    assert not _has_call(compute)


def test_inline_call_with_const_argument() -> None:
    @spasm.inline
    def compute(x):
        return add(x, 10)

    assert compute(3) == 13
    assert not _has_call(compute)


def test_inline_multiple_call_sites() -> None:
    @spasm.inline
    def compute(a, b):
        return double(a) + double(b)

    assert compute(3, 4) == 14
    assert not _has_call(compute)


def test_inline_callee_with_multiple_returns() -> None:
    @spasm.inline
    def compute(x):
        return sign(x) + 1

    assert compute(5) == 2
    assert compute(-5) == 0
    assert not _has_call(compute)


def test_inline_no_call_sites_returns_unchanged() -> None:
    @spasm.inline
    def plain(x):
        return x + 1

    assert plain(4) == 5


def test_inline_leaves_kwarg_call_untouched() -> None:
    @spasm.inline
    def compute(x):
        return add_kwarg(x, b=2)

    assert compute(5) == 7


def test_inline_leaves_varargs_callee_untouched() -> None:
    @spasm.inline
    def compute(x):
        return total(x, x)

    assert compute(3) == 6


def test_inline_leaves_generator_callee_untouched() -> None:
    @spasm.inline
    def compute(x):
        return list(gen(x))

    assert compute(5) == [5]


def test_inline_leaves_recursive_call_untouched() -> None:
    @spasm.inline
    def fact(n):
        if n <= 1:
            return 1
        return n * fact(n - 1)

    assert fact(5) == 120


def test_inline_propagates_readonly_simple_args() -> None:
    @spasm.inline
    def compute(n):
        return add_one(n) * 2

    assert [compute(n) for n in (0, 1, -3, 10)] == [2, 4, -4, 22]
    assert not _has_call(compute)
    # `x` is read-only in add_one and pushed by a plain LOAD_FAST, so it's
    # substituted at its use site instead of round-tripping through a
    # dedicated local.
    assert "__inline_0_x" not in compute.__code__.co_varnames


def test_inline_keeps_local_for_reassigned_param() -> None:
    @spasm.inline
    def compute(n):
        return bump(n)

    assert [compute(n) for n in (0, 1, -3, 10)] == [2, 4, -4, 22]
    assert not _has_call(compute)
    # `x` is reassigned inside bump, so it still needs its own local.
    assert "__inline_0_x" in compute.__code__.co_varnames


def test_inline_propagates_arg_used_multiple_times() -> None:
    @spasm.inline
    def compute(x):
        # A const second argument keeps the compiler from fusing the two
        # pushes into a single LOAD_FAST_LOAD_FAST, so both slots remain
        # eligible for propagation.
        return double_use(x, 10)

    assert compute(3) == 16
    assert compute(-2) == 6
    assert not _has_call(compute)
    assert "__inline_0_a" not in compute.__code__.co_varnames
    assert "__inline_0_b" not in compute.__code__.co_varnames


def test_inline_leaves_closure_callee_untouched() -> None:
    def make_adder(n):
        def adder(x):
            return x + n

        return adder

    add_five = make_adder(5)

    @spasm.inline
    def compute(x):
        return add_five(x)

    assert compute(3) == 8
