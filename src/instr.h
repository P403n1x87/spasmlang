#pragma once

#include <Python.h>
#include <cstdint>
#include <variant>
#include <vector>
#include <string>

// ── Label ────────────────────────────────────────────────────────────────────
// Opaque identifier for a jump target.  Labels are resolved to byte offsets
// during assembly and never appear in the final bytecode.
struct Label {
    int id;
    bool operator==(const Label& o) const noexcept { return id == o.id; }
};

// ── Location ─────────────────────────────────────────────────────────────────
struct Location {
    int lineno      = -1;
    int end_lineno  = -1;
    int col_offset  = -1;
    int end_col     = -1;
};

// ── Arg ──────────────────────────────────────────────────────────────────────
// An instruction argument is one of:
//   - NoArg     : instruction has no meaningful argument (arg byte is 0)
//   - int       : literal integer (stack effect, const index already resolved, …)
//   - Label     : symbolic jump target, resolved during assembly
//   - PyObject* : a Python object (constant); ref is borrowed from co_consts
//
// We use a borrowed-ref model: the Bytecode object owns co_consts and keeps it
// alive for the lifetime of all instructions referencing it.  If you build an
// Instr from scratch with a new constant you must ensure the object outlives
// the Bytecode.

struct NoArg {};

using Arg = std::variant<NoArg, int, Label, PyObject*>;

// ── Instr ────────────────────────────────────────────────────────────────────
// Labels that target this instruction (i.e. jump targets landing here) are
// carried directly as `labels`, rather than as separate zero-width anchor
// pseudo-instructions in the surrounding sequence. This keeps every entry in
// a Bytecode's instruction list a real instruction, and makes label targets
// travel with the instruction across insertions/removals for free.
struct Instr {
    uint8_t            op;
    Arg                arg;
    Location           loc;
    std::vector<Label> labels;

    // Convenience constructors
    explicit Instr(uint8_t op) noexcept
        : op(op), arg(NoArg{}) {}

    Instr(uint8_t op, int a) noexcept
        : op(op), arg(a) {}

    Instr(uint8_t op, Label lbl) noexcept
        : op(op), arg(lbl) {}

    Instr(uint8_t op, PyObject* obj) noexcept
        : op(op), arg(obj) {}

    bool has_arg() const noexcept {
        return !std::holds_alternative<NoArg>(arg);
    }

    // Returns the raw integer arg value, resolving Label and PyObject* is the
    // caller's responsibility.  Aborts if arg is a Label (use only after
    // resolution).
    int int_arg() const noexcept {
        if (auto* i = std::get_if<int>(&arg))   return *i;
        if (auto* o = std::get_if<PyObject*>(&arg)) return static_cast<int>(
            reinterpret_cast<uintptr_t>(o));   // should not happen post-resolve
        return 0;
    }
};

// ── InstrSlot ─────────────────────────────────────────────────────────────────
// Internal assembler state: one slot per logical instruction.
// n_extended tracks how many EXTENDED_ARG prefixes are needed; this grows
// monotonically during the relaxation loop and never shrinks.
struct InstrSlot {
    Instr    instr;
    uint8_t  n_extended = 0;   // 0–3
    uint32_t offset     = 0;   // byte offset of this instruction in the output
                                // (excluding its own EXTENDED_ARG prefixes)
};
