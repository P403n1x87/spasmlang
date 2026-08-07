#pragma once

#include "compat.h"
#include "instr.h"

#include <vector>
#include <cstdint>

// Sentinel for ExcEntry(L)::depth meaning "not yet known — compute_stacksize()
// should resolve it from the depth reached at `start_lbl` by normal control
// flow". Real depths are always >= 0, so -1 can't collide with one. Defined
// unconditionally: the Python ExcEntry type compiles on all versions (it's
// simply never populated with entries pre-3.11).
constexpr int EXC_DEPTH_AUTO = -1;

#if HAS_EXCEPTION_TABLE

// Raw entry as decoded/encoded from/to co_exceptiontable.
// All offsets are CACHE-inclusive byte offsets.
struct ExcEntry {
    uint32_t start_offset;   // inclusive
    uint32_t stop_offset;    // exclusive
    uint32_t handler_offset;
    int      depth;
    bool     lasti;
};

// Label-based entry — used as the live representation inside Bytecode.
// Labels are placed as anchors in the instruction stream; their offsets
// are resolved by to_code() using the same lmap as jump labels.
struct ExcEntryL {
    Label start_lbl;
    Label stop_lbl;    // exclusive end — anchor sits at the first instr AFTER the block
    Label handler_lbl;
    int   depth;
    bool  lasti;
};

// Decode co_exceptiontable into raw ExcEntry list.
std::vector<ExcEntry> decode_exctable(PyCodeObject* co);

// Encode resolved ExcEntry list to bytes.  Returns new ref or NULL.
PyObject* encode_exctable(const std::vector<ExcEntry>& entries);

#endif  // HAS_EXCEPTION_TABLE
