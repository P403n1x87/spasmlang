#pragma once

#include "compat.h"
#include "instr.h"
#include "exctable.h"

#include <vector>
#include <unordered_map>
#include <stdexcept>
#include <string>

// ── CodeMeta ──────────────────────────────────────────────────────────────────
// Note: no stacksize field — co_stacksize is always computed fresh by
// to_code() (see stackdepth.h), never stored or user-settable.
struct CodeMeta {
    int argcount        = 0;
    int posonlyargcount = 0;
    int kwonlyargcount  = 0;
    int nlocals         = 0;
    int flags           = 0;
    int firstlineno     = 1;

    // Mutable owned Python lists — allow the caller to append new entries.
    PyObject* consts   = nullptr;  // list  (was co_consts tuple)
    PyObject* names    = nullptr;  // list of str
    PyObject* varnames = nullptr;  // list of str
    PyObject* freevars = nullptr;  // list of str
    PyObject* cellvars = nullptr;  // list of str

    // Owned Python str objects. qualname defaults to name when the running
    // version has no co_qualname (3.10) or none was supplied — always safe
    // to read.
    PyObject* filename = nullptr;
    PyObject* name     = nullptr;
    PyObject* qualname = nullptr;

    CodeMeta() = default;
    CodeMeta(const CodeMeta&) = delete;
    CodeMeta& operator=(const CodeMeta&) = delete;

    CodeMeta(CodeMeta&& o) noexcept { *this = std::move(o); }
    CodeMeta& operator=(CodeMeta&& o) noexcept {
        #define MOVE_OWN(f) f = o.f; o.f = nullptr
        MOVE_OWN(consts); MOVE_OWN(names); MOVE_OWN(varnames);
        MOVE_OWN(freevars); MOVE_OWN(cellvars);
        MOVE_OWN(filename); MOVE_OWN(name); MOVE_OWN(qualname);
        #undef MOVE_OWN
        argcount = o.argcount; posonlyargcount = o.posonlyargcount;
        kwonlyargcount = o.kwonlyargcount; nlocals = o.nlocals;
        flags = o.flags; firstlineno = o.firstlineno;
        return *this;
    }
    ~CodeMeta() {
        Py_XDECREF(consts); Py_XDECREF(names);
        Py_XDECREF(varnames); Py_XDECREF(freevars); Py_XDECREF(cellvars);
        Py_XDECREF(filename); Py_XDECREF(name); Py_XDECREF(qualname);
    }
};

// ── Bytecode ──────────────────────────────────────────────────────────────────
// A mutable, label-aware instruction sequence.
//
// Labels targeting a real instruction are carried directly on it, via
// Instr::labels — there are no separate zero-width anchor pseudo-instructions
// in `instrs`. Every entry in `instrs` is always a real instruction. Labels
// that target the position one-past-the-last-instruction (used by, e.g., an
// exception table range that runs to the very end of the code with nothing
// after it) live in `end_labels` instead, since there is no instruction to
// attach them to.
//
// from_code() resolves every jump's raw integer offset to a Label pointing
// at its real target as part of decoding — using the exact byte layout
// already in hand, so there is no separate, error-prone "symbolify" pass
// to run afterward. Use new_label() to reserve an id for jumps you add
// yourself, then attach it to the target Instr's `.labels` (or push to
// `end_labels` for the one-past-the-end case).
// label_index_map() recomputes label id -> current instrs[] index in one
// O(n) pass whenever code needs to resolve "where is this label right now"
// — e.g. after edits have moved things around.
class Bytecode {
public:
    std::vector<Instr> instrs;
    CodeMeta           meta;

    // Labels targeting one-past-the-last-instruction. See class doc.
    std::vector<Label> end_labels;

#if HAS_EXCEPTION_TABLE
    // Label-based exception table entries — set by from_code(), used by to_code().
    std::vector<ExcEntryL> exc_labeled;
#endif

    // Next label id to allocate.
    int next_label_id = 0;

    // ── Construction ─────────────────────────────────────────────────────────
    static Bytecode from_code(PyCodeObject* co);

    // ── Assembly ──────────────────────────────────────────────────────────────
    // Resolves labels, runs the EXTENDED_ARG relaxation loop, encodes the
    // line/location table, and builds a new PyCodeObject.  Returns a new ref
    // or NULL with a Python exception set.
    PyObject* to_code() const;

    // ── Label helpers ─────────────────────────────────────────────────────────
    Label new_label();

    // Recompute label id -> current index in `instrs` in one O(n) pass, by
    // scanning each instruction's `.labels`. Labels in `end_labels` map to
    // instrs.size() (one past the last real instruction).
    std::unordered_map<int, size_t> label_index_map() const;

private:
    // Inline relaxation loop used by to_code() — not exposed as a static method.
};
