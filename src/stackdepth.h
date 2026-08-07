#pragma once

#include "compat.h"
#include "instr.h"
#include "exctable.h"

#include <vector>
#include <unordered_map>

// Compute the maximum value-stack depth reached while executing `instrs`,
// so callers never have to track/set co_stacksize by hand.
//
// Walks the instruction graph (jump edges resolved via `label_idx`, as
// produced by Bytecode::label_index_map(), plus a synthetic entry per
// exception handler) computing each instruction's push/pop effect via the
// running interpreter's own _opcode.stack_effect() — the same source of
// truth CPython's own compiler uses — so this never needs a hand-maintained
// per-opcode effect table and automatically tracks opcode changes across
// versions.
//
// Every jump instruction's arg must already be a Label (as from_code()
// guarantees, and as new_label()-based hand-built jumps naturally are).
//
// Any ExcEntryL whose `depth` is EXC_DEPTH_AUTO has it resolved in place
// (as the depth normal control flow reaches `start_lbl` at) — this is what
// lets hand-built ExcEntry objects leave `.depth` unset, the same way
// callers never need to set co_stacksize by hand. Entries with an explicit
// (>= 0) depth — e.g. those from_code() decoded from a real exception
// table — are left untouched and used as given.
//
// Returns -1 with a Python exception set on failure (unresolved label, a
// jump with a raw int arg, or an EXC_DEPTH_AUTO entry whose start_lbl is
// unreachable from normal control flow).
int compute_stacksize(const std::vector<Instr>& instrs,
                      const std::unordered_map<int, size_t>& label_idx
#if HAS_EXCEPTION_TABLE
                      , std::vector<ExcEntryL>& exc_labeled
#endif
                      );
