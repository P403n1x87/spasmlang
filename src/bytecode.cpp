#include "bytecode.h"
#include "linetable.h"
#include "stackdepth.h"
#include "arg_kind_gen.h"
#include "cache_sizes_gen.h"
#include "jump_opcodes_gen.h"
#include "stackdepth_opcodes_gen.h"

#include <cassert>
#include <stdexcept>

// ── Helpers ──────────────────────────────────────────────────────────────────

// Search lst for obj (identity then equality).  Returns index, or -1 if absent.
// Does NOT append — callers that need append use find_or_add().
static Py_ssize_t find_only(PyObject* lst, PyObject* obj)
{
    Py_ssize_t n = PyList_GET_SIZE(lst);
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* item = PyList_GET_ITEM(lst, i);
        if (item == obj) return i;
        int eq = PyObject_RichCompareBool(item, obj, Py_EQ);
        if (eq < 0) { PyErr_Clear(); continue; }
        if (eq) return i;
    }
    return -1;
}

// Find obj in a Python list by identity first, then type-strict equality.
// Append if absent.  Returns the index, or -1 on error.
// Type-strict equality (same type required) prevents conflating distinct
// constants like 0 (int) and False (bool), or 1 and True.
static Py_ssize_t find_or_add(PyObject* lst, PyObject* obj)
{
    Py_ssize_t n = PyList_GET_SIZE(lst);
    PyTypeObject* obj_type = Py_TYPE(obj);
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* item = PyList_GET_ITEM(lst, i);
        if (item == obj) return i;
        if (Py_TYPE(item) == obj_type) {
            int eq = PyObject_RichCompareBool(item, obj, Py_EQ);
            if (eq < 0) { PyErr_Clear(); continue; }
            if (eq) return i;
        }
    }
    if (PyList_Append(lst, obj) < 0) return -1;
    return n;
}

// ── localsplus addressing ────────────────────────────────────────────────────
// From 3.11 the oparg of every variable opcode — LOAD_FAST as much as
// LOAD_DEREF — indexes the frame's "localsplus" array rather than co_varnames
// or co_cellvars+co_freevars. That array is co_varnames, then the cells that
// are not already arguments, then the free variables: an argument captured by
// a nested function occupies a single slot that is both local and cell, which
// is why the cells have to be filtered rather than appended wholesale.
// PyCode_NewWithPosOnlyArgs builds it exactly this way.
//
// CPython has been reclassifying opcodes to match — LOAD_CLOSURE moved from
// hasfree to haslocal in 3.13 and LOAD_DEREF in 3.14 — so on 3.11+ neither
// ArgKind tells you which table a name lives in, and both have to go through
// the same lookup. Before 3.11 the two namespaces really are separate.
#if PY_VERSION_HEX >= PY_311
#  define UNIFIED_LOCALSPLUS 1
#else
#  define UNIFIED_LOCALSPLUS 0
#endif

#if UNIFIED_LOCALSPLUS
// The localsplus index of `name`, or -1 if it is in none of the tables.
static Py_ssize_t localsplus_find(const CodeMeta& meta, PyObject* name)
{
    Py_ssize_t i = find_only(meta.varnames, name);
    if (i >= 0) return i;

    Py_ssize_t slot   = PyList_GET_SIZE(meta.varnames);
    Py_ssize_t ncells = PyList_GET_SIZE(meta.cellvars);
    for (Py_ssize_t c = 0; c < ncells; ++c) {
        PyObject* cell = PyList_GET_ITEM(meta.cellvars, c);
        if (find_only(meta.varnames, cell) >= 0) continue;  // shares an argument's slot
        if (cell == name) return slot;
        int eq = PyObject_RichCompareBool(cell, name, Py_EQ);
        if (eq < 0) PyErr_Clear();
        else if (eq) return slot;
        ++slot;
    }

    Py_ssize_t f = find_only(meta.freevars, name);
    return f >= 0 ? slot + f : -1;
}

// The name at localsplus index `idx` (borrowed), or nullptr if out of range.
static PyObject* localsplus_name(const CodeMeta& meta, Py_ssize_t idx)
{
    if (idx < 0) return nullptr;

    Py_ssize_t nlocals = PyList_GET_SIZE(meta.varnames);
    if (idx < nlocals) return PyList_GET_ITEM(meta.varnames, idx);

    Py_ssize_t slot   = nlocals;
    Py_ssize_t ncells = PyList_GET_SIZE(meta.cellvars);
    for (Py_ssize_t c = 0; c < ncells; ++c) {
        PyObject* cell = PyList_GET_ITEM(meta.cellvars, c);
        if (find_only(meta.varnames, cell) >= 0) continue;
        if (slot == idx) return cell;
        ++slot;
    }

    Py_ssize_t f = idx - slot;
    if (f >= 0 && f < PyList_GET_SIZE(meta.freevars))
        return PyList_GET_ITEM(meta.freevars, f);
    return nullptr;
}
#endif

static inline uint8_t extended_args_needed(uint32_t arg) noexcept
{
    if (arg <= 0x0000'00FFu) return 0;
    if (arg <= 0x0000'FFFFu) return 1;
    if (arg <= 0x00FF'FFFFu) return 2;
    return 3;
}


// ════════════════════════════════════════════════════════════════════════════
// from_code — disassemble a PyCodeObject into a Bytecode
// ════════════════════════════════════════════════════════════════════════════

Bytecode Bytecode::from_code(PyCodeObject* co)
{
    Bytecode bc;

    // ── Metadata ──────────────────────────────────────────────────────────
    bc.meta.argcount        = co->co_argcount;
    bc.meta.posonlyargcount = co->co_posonlyargcount;
    bc.meta.kwonlyargcount  = co->co_kwonlyargcount;
    bc.meta.nlocals         = co->co_nlocals;
    bc.meta.flags           = co->co_flags;
    bc.meta.firstlineno     = co->co_firstlineno;
    bc.meta.filename        = co->co_filename;   Py_INCREF(bc.meta.filename);
    bc.meta.name            = co->co_name;       Py_INCREF(bc.meta.name);
#if PY_VERSION_HEX >= PY_311
    bc.meta.qualname        = co->co_qualname;   Py_INCREF(bc.meta.qualname);
#else
    // No co_qualname before 3.11 — fall back to name so the field is
    // never null.
    bc.meta.qualname        = bc.meta.name;      Py_INCREF(bc.meta.qualname);
#endif

    // Convert tuples → mutable Python lists so callers can extend them.
    // GET_CO_* returns new refs; PySequence_List steals nothing — we DECREF
    // the intermediate tuple/list returned by the accessor.
    auto tuple_to_list = [](PyObject* t) -> PyObject* {
        PyObject* lst = PySequence_List(t);
        Py_DECREF(t);
        return lst;
    };
    bc.meta.consts   = PySequence_List(co->co_consts);  // tuple → list
    bc.meta.names    = PySequence_List(co->co_names);
    bc.meta.varnames = tuple_to_list(GET_CO_VARNAMES(co));
    bc.meta.freevars = tuple_to_list(GET_CO_FREEVARS(co));
    bc.meta.cellvars = tuple_to_list(GET_CO_CELLVARS(co));
    if (!bc.meta.consts || !bc.meta.names || !bc.meta.varnames ||
        !bc.meta.freevars || !bc.meta.cellvars)
        throw std::runtime_error("failed to build metadata lists");

    // ── Raw bytecode ──────────────────────────────────────────────────────
    PyObject* code_bytes = GET_CODE_BYTES(co);
    if (!code_bytes) throw std::runtime_error("failed to get co_code");

    const uint8_t* raw = reinterpret_cast<const uint8_t*>(
                            PyBytes_AS_STRING(code_bytes));
    Py_ssize_t     nbytes = PyBytes_GET_SIZE(code_bytes);
    // Keep code_bytes alive until after exception table processing — we need
    // raw[] to build the CACHE-inclusive offset→index map (EXTENDED_ARGs must
    // be counted, which requires a second pass over the raw bytes).

    // ── Decode instructions, folding EXTENDED_ARG chains ──────────────────
    // First pass: collect instructions and their resolved args.
    // We temporarily store each decoded instruction; the location table is
    // decoded separately and merged below.
    //
    // Alongside each decoded instruction we record two byte offsets, needed
    // below to resolve jump targets and exception-table boundaries using
    // the *real* layout (no need to re-derive EXTENDED_ARG width later):
    //   idx_to_off[k]     - canonical offset: start of instruction k's own
    //                       EXTENDED_ARG chain if it has one, else its op byte.
    //                       This is the position any jump/label targeting
    //                       instruction k must resolve to.
    //   idx_to_nextoff[k] - offset right after instruction k's op word and
    //                       its CACHE entries — the "next instruction" base
    //                       relative jumps are computed from.
    bc.instrs.reserve(static_cast<size_t>(nbytes / INSTR_BYTES));
    std::vector<uint32_t> idx_to_off;
    std::vector<uint32_t> idx_to_nextoff;
    idx_to_off.reserve(static_cast<size_t>(nbytes / INSTR_BYTES));
    idx_to_nextoff.reserve(static_cast<size_t>(nbytes / INSTR_BYTES));

    int extended_arg = 0;
    int skip_cache   = 0;  // CACHE entries remaining to skip after current instr
    uint32_t ext_start = 0;

    for (Py_ssize_t i = 0; i < nbytes; i += INSTR_BYTES) {
        uint8_t op      = raw[i];
        uint8_t raw_arg = raw[i + 1];

#if HAS_CACHE_ENTRIES
        // CACHE instructions (opcode 0) immediately follow their owning
        // instruction.  Skip them; we re-insert them in to_code().
        if (op == 0 && extended_arg == 0) {
            if (skip_cache > 0) { --skip_cache; continue; }
            // op==0 here is a CACHE, not an EXTENDED_ARG — skip it.
            // (EXTENDED_ARG has opcode EXTENDED_ARG, not 0.)
            continue;
        }
#endif

        if (op == EXTENDED_ARG) {
            if (extended_arg == 0) ext_start = static_cast<uint32_t>(i);
            extended_arg = (extended_arg | raw_arg) << 8;
            continue;
        }

        uint32_t canonical_off = (extended_arg != 0) ? ext_start : static_cast<uint32_t>(i);
        int arg = extended_arg | raw_arg;
        extended_arg = 0;

        // Abstract decode: replace index arg with the actual Python value.
        // Bounds-check before PyList_GET_ITEM; fall back to raw int on any
        // out-of-range access (keeps round-trip safe for edge cases).
        switch (arg_kind(op)) {
        case ArgKind::CONST: {
            Py_ssize_t n = PyList_GET_SIZE(bc.meta.consts);
            if (arg >= 0 && arg < n) {
                bc.instrs.emplace_back(op, PyList_GET_ITEM(bc.meta.consts, arg));
            } else {
                bc.instrs.emplace_back(op, arg);
            }
            break;
        }
        case ArgKind::LOCAL:
        case ArgKind::FREE: {
#if UNIFIED_LOCALSPLUS
            PyObject* nm = localsplus_name(bc.meta, arg);
            if (nm) {
                bc.instrs.emplace_back(op, nm);
            } else {
                bc.instrs.emplace_back(op, arg);
            }
#else
            if (arg_kind(op) == ArgKind::LOCAL) {
                Py_ssize_t n = PyList_GET_SIZE(bc.meta.varnames);
                if (arg >= 0 && arg < n) {
                    bc.instrs.emplace_back(op, PyList_GET_ITEM(bc.meta.varnames, arg));
                } else {
                    bc.instrs.emplace_back(op, arg);
                }
            } else {
                Py_ssize_t ncells = PyList_GET_SIZE(bc.meta.cellvars);
                Py_ssize_t nfree  = PyList_GET_SIZE(bc.meta.freevars);
                if (arg >= 0 && arg < ncells) {
                    bc.instrs.emplace_back(op, PyList_GET_ITEM(bc.meta.cellvars, arg));
                } else if (arg >= ncells && arg - ncells < nfree) {
                    bc.instrs.emplace_back(op, PyList_GET_ITEM(bc.meta.freevars, arg - ncells));
                } else {
                    bc.instrs.emplace_back(op, arg);
                }
            }
#endif
            break;
        }
        default:
            bc.instrs.emplace_back(op, arg);
            break;
        }

        skip_cache = instr_cache_size(op);
        idx_to_off.push_back(canonical_off);
        idx_to_nextoff.push_back(static_cast<uint32_t>(i) + INSTR_BYTES
                                  + static_cast<uint32_t>(skip_cache) * INSTR_BYTES);
    }

    // ── Location info ─────────────────────────────────────────────────────
    // Pass raw bytes so decode_linetable can correlate code units with logical
    // instructions (each linetable entry's `length` counts code units, not
    // logical instructions — CACHE and EXTENDED_ARG words must be skipped).
    auto locs = decode_linetable(co, raw, nbytes);
    int n = static_cast<int>(bc.instrs.size());
    if (n > static_cast<int>(locs.size())) n = static_cast<int>(locs.size());
    for (int i = 0; i < n; ++i)
        bc.instrs[static_cast<size_t>(i)].loc = locs[static_cast<size_t>(i)];

    // ── Shared byte-offset → instruction-index map ────────────────────────
    // Built once from idx_to_off, reused below for both jump-target
    // resolution and exception-table boundary resolution. A virtual entry
    // at nbytes covers targets landing exactly one-past-the-end.
    std::unordered_map<uint32_t, size_t> off_to_idx;
    for (size_t k = 0; k < idx_to_off.size(); ++k)
        off_to_idx[idx_to_off[k]] = k;
    off_to_idx[static_cast<uint32_t>(nbytes)] = idx_to_off.size();

    // ── Jump targets → Labels ──────────────────────────────────────────────
    // Every jump instruction's raw integer offset is replaced with a Label
    // attached directly to its target instruction (or end_labels), using
    // the exact offsets computed above — no heuristics, no separate
    // symbolification pass needed later.
    {
        std::unordered_map<uint32_t, Label> offset_to_label;
        for (size_t k = 0; k < bc.instrs.size(); ++k) {
            Instr& instr = bc.instrs[k];
            if (!is_jump_opcode(instr.op)) continue;
            auto* iv = std::get_if<int>(&instr.arg);
            if (!iv) continue;

            uint32_t arg_val = static_cast<uint32_t>(*iv);
            JumpKind jk = jump_kind(instr.op);
            uint32_t target;
            if (jk == JumpKind::ABS || jk == JumpKind::NONE) {
                target = ARG_TO_BYTE_OFFSET(arg_val);
            } else if (jk == JumpKind::FWD) {
                target = idx_to_nextoff[k] + ARG_TO_BYTE_OFFSET(arg_val);
            } else {  // BWD
                target = idx_to_nextoff[k] - ARG_TO_BYTE_OFFSET(arg_val);
            }

            auto lit = offset_to_label.find(target);
            if (lit != offset_to_label.end()) {
                instr.arg = lit->second;
                continue;
            }

            Label lbl = bc.new_label();
            offset_to_label[target] = lbl;
            instr.arg = lbl;

            auto it = off_to_idx.find(target);
            if (it == off_to_idx.end()) continue;  // malformed bytecode; skip defensively
            if (it->second < bc.instrs.size())
                bc.instrs[it->second].labels.push_back(lbl);
            else
                bc.end_labels.push_back(lbl);
        }
    }

    // ── Exception table ───────────────────────────────────────────────────
#if HAS_EXCEPTION_TABLE
    {
        auto raw_entries = decode_exctable(co);

        // Per-offset label cache: reuse labels when two entries share a
        // boundary (including boundaries already labeled by a jump above).
        std::unordered_map<uint32_t, Label> off_to_label;

        auto get_or_create_label = [&](uint32_t byte_off) -> Label {
            auto it = off_to_label.find(byte_off);
            if (it != off_to_label.end()) return it->second;
            Label lbl = bc.new_label();
            off_to_label[byte_off] = lbl;
            return lbl;
        };

        for (const auto& e : raw_entries) {
            ExcEntryL el;
            el.start_lbl   = get_or_create_label(e.start_offset);
            el.stop_lbl    = get_or_create_label(e.stop_offset);
            el.handler_lbl = get_or_create_label(e.handler_offset);
            el.depth = e.depth;
            el.lasti = e.lasti;
            bc.exc_labeled.push_back(el);
        }

        // Attach each label directly to its target instruction (or to
        // end_labels for the one-past-the-end sentinel index).
        for (auto& [off, lbl] : off_to_label) {
            auto it = off_to_idx.find(off);
            if (it == off_to_idx.end()) continue;
            if (it->second < bc.instrs.size())
                bc.instrs[it->second].labels.push_back(lbl);
            else
                bc.end_labels.push_back(lbl);
        }
    }
#endif

    // Deferred: raw bytes no longer needed.
    Py_DECREF(code_bytes);

    return bc;
}

// ════════════════════════════════════════════════════════════════════════════
// label_index_map
// ════════════════════════════════════════════════════════════════════════════
// Jump targets are already resolved to Labels by from_code() itself (see
// above) — there's no separate symbolification step to run later.

std::unordered_map<int, size_t> Bytecode::label_index_map() const
{
    std::unordered_map<int, size_t> m;
    for (size_t i = 0; i < instrs.size(); ++i)
        for (const auto& lbl : instrs[i].labels)
            m[lbl.id] = i;
    for (const auto& lbl : end_labels)
        m[lbl.id] = instrs.size();
    return m;
}

// ════════════════════════════════════════════════════════════════════════════
// to_code — assemble a Bytecode back into a PyCodeObject
// ════════════════════════════════════════════════════════════════════════════

PyObject* Bytecode::to_code() const
{
    // ── Reject opcodes that cannot appear in an assembled code object ───────
    // The INSTRUMENTED_* family and ENTER_EXECUTOR are written into co_code by
    // the interpreter itself and refer to state (monitoring tables, executors)
    // that a code object being built does not have. Passing one to PyCode_New
    // segfaults, so fail here with an exception instead.
    for (const auto& instr : instrs) {
        if (is_internal_opcode(instr.op)) {
            PyErr_Format(PyExc_ValueError,
                "opcode %d is internal to the interpreter and cannot be assembled",
                static_cast<int>(instr.op));
            return nullptr;
        }
    }

    // ── Compute co_stacksize ────────────────────────────────────────────────
    // Always computed fresh here — never stored or user-settable — so
    // callers never need to track an appropriate value themselves, even
    // after arbitrary edits to .instrs. See stackdepth.h.
    auto label_idx = label_index_map();
#if HAS_EXCEPTION_TABLE
    // compute_stacksize() resolves any EXC_DEPTH_AUTO entries in place, so
    // it needs a mutable copy — this method is const, and exc_labeled with
    // explicit depths (e.g. from_code()'s) must not be touched.
    auto exc_local = exc_labeled;
#endif
    int stacksize = compute_stacksize(instrs, label_idx
#if HAS_EXCEPTION_TABLE
        , exc_local
#endif
    );
    if (stacksize < 0) return nullptr;  // exception already set

    // ── Build initial slot list ───────────────────────────────────────────
    // For integer args, set n_extended upfront — these are fixed values that
    // don't change during relaxation (unlike Label args whose encoded offset
    // can grow as the layout shifts).
    std::vector<InstrSlot> slots;
    slots.reserve(instrs.size());
    for (const auto& instr : instrs) {
        uint8_t n_ext = 0;
        if (auto* iv = std::get_if<int>(&instr.arg))
            n_ext = extended_args_needed(static_cast<uint32_t>(*iv));
        slots.push_back(InstrSlot{instr, n_ext, 0});
    }

    // ── Pre-pass: resolve PyObject* args to integer indices ──────────────
    // CONST/LOCAL/FREE args are stored as Python objects after abstract decode.
    // We need their table indices to correctly compute n_extended before the
    // relaxation loop runs (a LOAD_CONST with index 300 needs 1 EXTENDED_ARG,
    // and that affects ALL subsequent instruction offsets and Label resolutions).
    //
    // Variable names are interned first and indexed second, in two separate
    // sweeps: adding a name to co_varnames shifts the localsplus index of every
    // cell and free variable after it, so no index is meaningful until every
    // name is in place.
    for (auto& slot : slots) {
        if (slot.instr.op == 0) continue;
        if (!std::holds_alternative<PyObject*>(slot.instr.arg)) continue;

        PyObject* pv = std::get<PyObject*>(slot.instr.arg);
        switch (arg_kind(slot.instr.op)) {
        case ArgKind::LOCAL:
#if UNIFIED_LOCALSPLUS
            // A LOCAL-kind argument may name a cell or a free variable — from
            // 3.13 LOAD_CLOSURE is classified this way, and from 3.14 so is
            // LOAD_DEREF — so it only becomes a new plain local if it is in
            // none of the tables.
            if (localsplus_find(meta, pv) < 0 && find_or_add(meta.varnames, pv) < 0) return nullptr;
#else
            if (find_or_add(meta.varnames, pv) < 0) return nullptr;
#endif
            break;
        case ArgKind::FREE:
            // Never append to cellvars: a name in neither table is a free
            // variable, and making it a cell would silently change what the
            // code object closes over.
            if (find_only(meta.cellvars, pv) < 0 && find_only(meta.freevars, pv) < 0
                && find_or_add(meta.freevars, pv) < 0)
                return nullptr;
            break;
        default:
            break;
        }
    }

    for (auto& slot : slots) {
        if (slot.instr.op == 0) continue;
        if (!std::holds_alternative<PyObject*>(slot.instr.arg)) continue;

        PyObject* pv = std::get<PyObject*>(slot.instr.arg);
        Py_ssize_t idx = -1;
        ArgKind kind = arg_kind(slot.instr.op);
        switch (kind) {
        case ArgKind::CONST:
            idx = find_or_add(meta.consts, pv);
            break;
        case ArgKind::LOCAL:
        case ArgKind::FREE:
#if UNIFIED_LOCALSPLUS
            idx = localsplus_find(meta, pv);
#else
            if (kind == ArgKind::LOCAL) {
                idx = find_only(meta.varnames, pv);
            } else {
                idx = find_only(meta.cellvars, pv);
                if (idx < 0) {
                    Py_ssize_t fi = find_only(meta.freevars, pv);
                    if (fi >= 0) idx = PyList_GET_SIZE(meta.cellvars) + fi;
                }
            }
#endif
            break;
        default:
            continue;
        }
        if (idx < 0) {
            // The interning sweep above should have made this unreachable;
            // fail loudly rather than encode a bogus index.
            if (!PyErr_Occurred())
                PyErr_Format(PyExc_ValueError, "cannot resolve variable argument %R", pv);
            return nullptr;
        }

        // Replace PyObject* arg with resolved integer index and update n_extended.
        slot.instr.arg = static_cast<int>(idx);
        slot.n_extended = extended_args_needed(static_cast<uint32_t>(idx));
    }

    // ── Relaxation: label maps rebuilt each iteration ─────────────────────
    // Every slot is a real instruction now (no zero-width anchors) — a
    // label's byte offset is simply the offset of the instruction it's
    // attached to. end_labels resolve to total_bytes, the one-past-the-end
    // position, computed fresh each iteration below.

    uint32_t total_bytes = 0;

    auto compute_label_map = [&]() {
        std::unordered_map<int,uint32_t> m;
        for (const auto& slot : slots)
            for (const auto& lbl : slot.instr.labels)
                m[lbl.id] = slot.offset;
        for (const auto& lbl : end_labels)
            m[lbl.id] = total_bytes;
        return m;
    };

    // ── Relaxation ────────────────────────────────────────────────────────
    bool changed = true;

    while (changed) {
        changed = false;

        // Assign offsets (including CACHE entries in the byte count so that
        // jump args resolve to the correct CACHE-inclusive positions).
        uint32_t off = 0;
        for (auto& slot : slots) {
            slot.offset = off;
            off += static_cast<uint32_t>(slot.n_extended + 1) * INSTR_BYTES;
#if HAS_CACHE_ENTRIES
            off += static_cast<uint32_t>(instr_cache_size(slot.instr.op)) * INSTR_BYTES;
#endif
        }
        total_bytes = off;

        auto lmap = compute_label_map();

        // Check and grow n_extended for jump instructions. Must mirror the
        // emit loop's arg computation exactly: for relative jumps (FWD/BWD)
        // the encoded arg is the *distance* to/from the next instruction,
        // not the target's absolute offset — using the absolute offset
        // here would silently under-grow EXTENDED_ARG for any backward
        // jump whose target offset is small but whose distance is large
        // (e.g. a big module-level code object jumping back near its start
        // from near its end).
        for (auto& slot : slots) {
            if (auto* lbl = std::get_if<Label>(&slot.instr.arg)) {
                auto it = lmap.find(lbl->id);
                if (it == lmap.end()) {
                    PyErr_Format(PyExc_ValueError, "unresolved label id %d", lbl->id);
                    return nullptr;
                }
                uint32_t target_byte = it->second;
                JumpKind jk = jump_kind(slot.instr.op);
                uint32_t arg_val;
                if (jk == JumpKind::ABS || jk == JumpKind::NONE) {
                    arg_val = BYTE_OFFSET_TO_ARG(target_byte);
                } else {
                    uint32_t ncache  = static_cast<uint32_t>(instr_cache_size(slot.instr.op));
                    uint32_t next_by = slot.offset
                                     + static_cast<uint32_t>(slot.n_extended + 1) * INSTR_BYTES
                                     + ncache * INSTR_BYTES;
                    uint32_t diff_by = (jk == JumpKind::FWD)
                                     ? (target_byte - next_by)
                                     : (next_by - target_byte);
                    arg_val = BYTE_OFFSET_TO_ARG(diff_by);
                }
                uint8_t needed = extended_args_needed(arg_val);
                if (needed > slot.n_extended) {
                    slot.n_extended = needed;
                    changed = true;
                }
            }
        }
    }

    auto lmap = compute_label_map();

    // ── Emit bytecode words ───────────────────────────────────────────────
    // total_bytes accounts only for logical instructions; CACHE entries are
    // appended below and don't participate in the relaxation layout.
    std::vector<uint8_t> code;
    code.reserve(total_bytes);

    for (const auto& slot : slots) {
        // Resolve arg value.
        // PyObject* args were already resolved to integer indices in the pre-pass.
        uint32_t arg_val = 0;
        if (auto* i = std::get_if<int>(&slot.instr.arg)) {
            arg_val = static_cast<uint32_t>(*i);
        } else if (auto* lbl = std::get_if<Label>(&slot.instr.arg)) {
            uint32_t target_byte = lmap.at(lbl->id);
            JumpKind jk = jump_kind(slot.instr.op);
            if (jk == JumpKind::ABS || jk == JumpKind::NONE) {
                arg_val = BYTE_OFFSET_TO_ARG(target_byte);
            } else {
                // Relative jump: arg = |target - next_instr| in word units.
                uint32_t ncache   = static_cast<uint32_t>(instr_cache_size(slot.instr.op));
                uint32_t next_by  = slot.offset
                                  + static_cast<uint32_t>(slot.n_extended + 1) * INSTR_BYTES
                                  + ncache * INSTR_BYTES;
                uint32_t diff_by  = (jk == JumpKind::FWD)
                                  ? (target_byte - next_by)
                                  : (next_by - target_byte);
                arg_val = BYTE_OFFSET_TO_ARG(diff_by);
            }
        }
        // PyObject* args should be resolved to indices before to_code().

        // Emit EXTENDED_ARG prefixes (most-significant first).
        uint8_t shifts = slot.n_extended;
        while (shifts > 0) {
            uint8_t ext_arg = static_cast<uint8_t>(arg_val >> (shifts * 8));
            code.push_back(EXTENDED_ARG);
            code.push_back(ext_arg);
            --shifts;
        }
        code.push_back(slot.instr.op);
        code.push_back(static_cast<uint8_t>(arg_val & 0xFF));

#if HAS_CACHE_ENTRIES
        // Re-insert CACHE (opcode 0, arg 0) entries stripped during from_code().
        int ncache = instr_cache_size(slot.instr.op);
        for (int c = 0; c < ncache; ++c) {
            code.push_back(0);  // CACHE opcode
            code.push_back(0);
        }
#endif
    }

    // ── Encode location table ─────────────────────────────────────────────
    // The location table has one entry per *word* in the code stream
    // (including CACHE words and EXTENDED_ARG words).
    // Expand each logical slot into: EXTENDED_ARG words + 1 instruction + ncache words.
    std::vector<InstrSlot> real_slots;
    real_slots.reserve(slots.size() * 4);
    for (const auto& slot : slots) {
        // Virtual slots for EXTENDED_ARG prefix words (same location as instruction).
        // slot.offset is the start of the EXTENDED_ARG chain.
        for (uint8_t e = 0; e < slot.n_extended; ++e) {
            InstrSlot es{Instr(EXTENDED_ARG, 0), 0, 0};
            es.instr.loc  = slot.instr.loc;
            es.n_extended = 0;
            es.offset     = slot.offset + static_cast<uint32_t>(e) * INSTR_BYTES;
            real_slots.push_back(es);
        }

        // The instruction itself (positioned after its EXTENDED_ARGs).
        InstrSlot main_slot = slot;
        main_slot.n_extended = 0;
        main_slot.offset = slot.offset + slot.n_extended * INSTR_BYTES;
        real_slots.push_back(main_slot);

#if HAS_CACHE_ENTRIES
        int ncache = instr_cache_size(slot.instr.op);
        for (int c = 0; c < ncache; ++c) {
            InstrSlot cs{Instr(0, 0), 0, 0};
            cs.instr.loc  = slot.instr.loc;
            cs.n_extended = 0;
            cs.offset     = main_slot.offset + static_cast<uint32_t>(1 + c) * INSTR_BYTES;
            real_slots.push_back(cs);
        }
#endif
    }

    PyObject* linetable = encode_linetable(real_slots, meta.firstlineno);
    if (!linetable) return nullptr;

#if HAS_EXCEPTION_TABLE
    // Resolve ExcEntryL labels → byte offsets using the now-final lmap.
    std::vector<ExcEntry> exc_resolved;
    exc_resolved.reserve(exc_local.size());
    for (const auto& el : exc_local) {
        auto resolve = [&](const Label& lbl) -> uint32_t {
            auto it = lmap.find(lbl.id);
            if (it == lmap.end()) {
                PyErr_Format(PyExc_ValueError,
                    "exception table label id=%d not found", lbl.id);
                return static_cast<uint32_t>(-1);
            }
            return it->second;
        };
        ExcEntry e;
        e.start_offset   = resolve(el.start_lbl);
        e.stop_offset    = resolve(el.stop_lbl);
        e.handler_offset = resolve(el.handler_lbl);
        // Any unresolved label leaves a ValueError set; bail out on any -1.
        if (e.start_offset   == static_cast<uint32_t>(-1) ||
            e.stop_offset    == static_cast<uint32_t>(-1) ||
            e.handler_offset == static_cast<uint32_t>(-1)) {
            Py_DECREF(linetable);
            return nullptr;
        }
        e.depth = el.depth;
        e.lasti = el.lasti;
        exc_resolved.push_back(e);
    }
    PyObject* exctable = encode_exctable(exc_resolved);
    if (!exctable) { Py_DECREF(linetable); return nullptr; }
#endif

    // ── Build PyCodeObject ────────────────────────────────────────────────
    PyObject* code_bytes = PyBytes_FromStringAndSize(
        reinterpret_cast<const char*>(code.data()),
        static_cast<Py_ssize_t>(code.size()));
    if (!code_bytes) {
        Py_DECREF(linetable);
#if HAS_EXCEPTION_TABLE
        Py_DECREF(exctable);
#endif
        return nullptr;
    }

    // co_nlocals must equal len(co_varnames); recompute in case new locals were added.
    const_cast<CodeMeta&>(meta).nlocals =
        static_cast<int>(PyList_GET_SIZE(meta.varnames));

    // PY_CODE_NEW_FN expects tuples for the table arguments.
    PyObject* consts_t   = PyList_AsTuple(meta.consts);
    PyObject* names_t    = PyList_AsTuple(meta.names);
    PyObject* varnames_t = PyList_AsTuple(meta.varnames);
    PyObject* freevars_t = PyList_AsTuple(meta.freevars);
    PyObject* cellvars_t = PyList_AsTuple(meta.cellvars);

    auto cleanup_tuples = [&] {
        Py_XDECREF(consts_t); Py_XDECREF(names_t); Py_XDECREF(varnames_t);
        Py_XDECREF(freevars_t); Py_XDECREF(cellvars_t);
    };

    if (!consts_t || !names_t || !varnames_t || !freevars_t || !cellvars_t) {
        cleanup_tuples();
        Py_DECREF(code_bytes); Py_DECREF(linetable);
#if HAS_EXCEPTION_TABLE
        Py_DECREF(exctable);
#endif
        return nullptr;
    }

    PyObject* result = nullptr;

#if PY_VERSION_HEX >= PY_311
    result = (PyObject*)PY_CODE_NEW_FN(
        meta.argcount,
        meta.posonlyargcount,
        meta.kwonlyargcount,
        meta.nlocals,
        stacksize,
        meta.flags,
        code_bytes,
        consts_t,
        names_t,
        varnames_t,
        freevars_t,
        cellvars_t,
        meta.filename,
        meta.name,
        meta.qualname,
        meta.firstlineno,
        linetable,
        exctable);
#else  // 3.10
    result = (PyObject*)PyCode_NewWithPosOnlyArgs(
        meta.argcount,
        meta.posonlyargcount,
        meta.kwonlyargcount,
        meta.nlocals,
        stacksize,
        meta.flags,
        code_bytes,
        consts_t,
        names_t,
        varnames_t,
        freevars_t,
        cellvars_t,
        meta.filename,
        meta.name,
        meta.firstlineno,
        linetable);  // lnotab on 3.10
#endif

    cleanup_tuples();

    Py_DECREF(code_bytes);
    Py_DECREF(linetable);
#if HAS_EXCEPTION_TABLE
    Py_DECREF(exctable);
#endif

    return result;
}

// ── Label helpers ─────────────────────────────────────────────────────────────

Label Bytecode::new_label()
{
    return Label{next_label_id++};
}
