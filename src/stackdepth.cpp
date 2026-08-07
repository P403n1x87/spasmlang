#include "stackdepth.h"
#include "jump_opcodes_gen.h"
#include "stackdepth_opcodes_gen.h"

#include <algorithm>
#include <limits>

namespace {

// Cache the _opcode.stack_effect callable for the process lifetime — this
// intentionally never releases the reference, same as caching any other
// process-lifetime singleton.
PyObject* stack_effect_callable()
{
    static PyObject* fn = nullptr;
    if (!fn) {
        PyObject* mod = PyImport_ImportModule("_opcode");
        if (!mod) return nullptr;
        fn = PyObject_GetAttrString(mod, "stack_effect");
        Py_DECREF(mod);
    }
    return fn;
}

// jump: -1 = omit the keyword (opcode isn't a jump), 0 = False, 1 = True.
// Returns false with a Python exception set on failure.
bool stack_effect(uint8_t op, int arg, int jump, int& out)
{
    PyObject* fn = stack_effect_callable();
    if (!fn) return false;

    PyObject* args = opcode_has_arg(op)
        ? Py_BuildValue("(ii)", static_cast<int>(op), arg)
        : Py_BuildValue("(i)", static_cast<int>(op));
    if (!args) return false;

    PyObject* kwargs = nullptr;
    if (jump >= 0) {
        kwargs = PyDict_New();
        if (!kwargs) { Py_DECREF(args); return false; }
        if (PyDict_SetItemString(kwargs, "jump", jump ? Py_True : Py_False) < 0) {
            Py_DECREF(args);
            Py_DECREF(kwargs);
            return false;
        }
    }

    PyObject* result = PyObject_Call(fn, args, kwargs);
    Py_DECREF(args);
    Py_XDECREF(kwargs);
    if (!result) return false;

    long v = PyLong_AsLong(result);
    Py_DECREF(result);
    if (v == -1 && PyErr_Occurred()) return false;
    out = static_cast<int>(v);
    return true;
}

} // namespace

int compute_stacksize(const std::vector<Instr>& instrs,
                      const std::unordered_map<int, size_t>& label_idx
#if HAS_EXCEPTION_TABLE
                      , std::vector<ExcEntryL>& exc_labeled
#endif
                      )
{
    size_t n = instrs.size();
    // Sentinel for "not yet visited". Must be a value no real depth can
    // ever equal — depths can legitimately go transiently negative (e.g.
    // 3.10's GEN_START pops before the generator body's first real push),
    // so -1 is not a safe sentinel here.
    constexpr int UNVISITED = std::numeric_limits<int>::min();
    std::vector<int> visited(n, UNVISITED);

    // Per-instruction worklist DFS (mirrors CPython's own per-basic-block
    // calculate_stackdepth() in Python/flowgraph.c). A node is only
    // (re-)pushed when the newly proposed entry depth is strictly greater
    // than what it was already explored with, so this naturally reaches a
    // fixed point on graphs with back edges (loops) under the same
    // assumption CPython's compiler makes: cycles have no net effect on
    // stack depth.
    std::vector<std::pair<size_t, int>> worklist;
    worklist.emplace_back(0, 0);

#if HAS_EXCEPTION_TABLE
    // Exception handlers are reached by the interpreter's unwinder, not by
    // any instruction's jump edge, so seed the worklist with one entry per
    // handler. CPython's assembler stores the *inverse* of the handler's
    // start depth in the exception table (h_startdepth - 1, and another -1
    // if lasti is preserved) — see Python/assemble.c — so invert that back
    // here to get the depth the handler actually starts executing at.
    //
    // An entry whose depth is EXC_DEPTH_AUTO can't be seeded yet — its
    // depth is only known once normal control flow has reached its
    // start_lbl, which may not happen until the drain below runs (or, for
    // a try block nested inside another handler, until *that* handler's
    // seed has been resolved and drained). Those are seeded lazily, below.
    auto seed_handler = [&](const ExcEntryL& e, int start_depth) -> bool {
        auto it = label_idx.find(e.handler_lbl.id);
        if (it == label_idx.end()) {
            PyErr_SetString(PyExc_ValueError,
                "cannot compute stack size: unresolved exception handler label");
            return false;
        }
        worklist.emplace_back(it->second, start_depth + 1 + (e.lasti ? 1 : 0));
        return true;
    };

    for (const auto& e : exc_labeled) {
        if (e.depth == EXC_DEPTH_AUTO) continue;
        if (!seed_handler(e, e.depth)) return -1;
    }
#endif

    int maxdepth = 0;
    auto drain = [&]() -> bool {
        while (!worklist.empty()) {
            auto [idx, depth] = worklist.back();
            worklist.pop_back();

            if (idx >= n) {
                // Fell off the end (e.g. a label in end_labels) — nothing more
                // executes on this path.
                if (depth > maxdepth) maxdepth = depth;
                continue;
            }
            if (depth <= visited[idx]) continue;
            visited[idx] = depth;
            if (depth > maxdepth) maxdepth = depth;

            const Instr& instr = instrs[idx];
            uint8_t op = instr.op;

            // The stack effect of every opcode that can carry a Label,
            // PyObject*, or NoArg arg doesn't depend on the actual value — only
            // (for jumps) on which edge is being computed — so 0 is always a
            // safe placeholder for those.
            int arg_val = 0;
            if (auto* iv = std::get_if<int>(&instr.arg)) arg_val = *iv;

            if (is_jump_opcode(op)) {
                int effect_taken;
                if (!stack_effect(op, arg_val, 1, effect_taken)) return false;
                int depth_taken = depth + effect_taken;
                if (depth_taken > maxdepth) maxdepth = depth_taken;

                size_t target_idx;
                if (auto* lbl = std::get_if<Label>(&instr.arg)) {
                    auto it = label_idx.find(lbl->id);
                    if (it == label_idx.end()) {
                        PyErr_Format(PyExc_ValueError,
                            "cannot compute stack size: unresolved jump label id %d", lbl->id);
                        return false;
                    }
                    target_idx = it->second;
                } else {
                    PyErr_SetString(PyExc_ValueError,
                        "cannot compute stack size: jump instruction has a raw int "
                        "arg instead of a Label — build jumps with Label targets "
                        "(see Bytecode.new_label())");
                    return false;
                }
                worklist.emplace_back(target_idx, depth_taken);

                if (!is_unconditional_jump(op)) {
                    int effect_fall;
                    if (!stack_effect(op, arg_val, 0, effect_fall)) return false;
                    worklist.emplace_back(idx + 1, depth + effect_fall);
                }
            } else if (is_stackdepth_neutral(op)) {
                // Generator-entry marker (GEN_START / RETURN_GENERATOR): its
                // real effect models what happens when the generator function
                // is *called*, but the body that follows always starts fresh
                // at the incoming depth when later resumed — don't accumulate
                // its effect. RETURN_GENERATOR is always immediately followed
                // by a cleanup POP_TOP in CPython's own generated prologue;
                // that pop must be neutralized too, or the offset comes right
                // back via the very next instruction.
                size_t next_idx = idx + 1;
                if (next_idx < n && instrs[next_idx].op == POP_TOP_OPCODE)
                    next_idx += 1;
                worklist.emplace_back(next_idx, depth);
            } else {
                int effect;
                if (!stack_effect(op, arg_val, -1, effect)) return false;
                int depth_next = depth + effect;
                if (depth_next > maxdepth) maxdepth = depth_next;
                if (!is_scope_exit(op)) {
                    worklist.emplace_back(idx + 1, depth_next);
                }
            }
        }
        return true;
    };

    if (!drain()) return -1;

#if HAS_EXCEPTION_TABLE
    // Resolve any EXC_DEPTH_AUTO entries now that normal control flow has
    // been traced.
    //
    // What CPython records in the exception table is the *block-nesting base
    // depth* of the try block the handler belongs to — a quantity its
    // compiler reads off the SETUP_FINALLY/POP_BLOCK pseudo-instructions in
    // its pre-assembly IR (Python/flowgraph.c, label_exception_targets).
    // Those pseudo-ops are consumed by the assembler, so that base depth is
    // *not*, in general, recoverable from an already-assembled table: in a
    // compiler-generated cleanup chain the correct answer can be 0 while
    // every instruction in the protected region genuinely runs at depth 2+.
    //
    // What *is* recoverable is the base depth of a try block whose protected
    // region normal control flow actually walks into: there the base is the
    // minimum depth reached across the region. So auto-depth is deliberately
    // exact-or-error — it infers a depth only for entries it can infer
    // correctly, and raises for the rest rather than silently emitting a
    // too-high depth (which would leave stale values below the handler on
    // unwind). Entries decoded from a real code object already carry an
    // explicit depth and never take this path.
    //
    // A region's minimum isn't simply the depth at its start_lbl — code in
    // the region can transiently sit lower than where it began — so each
    // entry's candidate is the minimum, over every instruction in
    // [start_idx, stop_idx), of that instruction's entering depth *and*
    // (when control leaves the region there — via fallthrough past
    // stop_idx, a jump out, or a scope exit) its post-effect exit depth.
    //
    // Entries are only assigned a final depth — and their shared handler
    // only seeded — once every entry sharing that handler has a candidate.
    // Seeding early with a value that later turns out too high would have
    // already driven downstream exploration (which only ever raises a
    // visited[] depth, never lowers it) to the wrong depths.

    // Reachability from the function entry following only ordinary control
    // flow — no exception-handler edges. An AUTO entry whose region is
    // reachable *only* by unwinding into a handler is exactly the
    // cleanup-chain case whose base depth was lost at assembly time, so it
    // is rejected below rather than guessed at.
    std::vector<bool> normally_reachable(n, false);
    {
        std::vector<size_t> stack;
        if (n) stack.push_back(0);
        while (!stack.empty()) {
            size_t idx = stack.back();
            stack.pop_back();
            if (idx >= n || normally_reachable[idx]) continue;
            normally_reachable[idx] = true;

            const Instr& instr = instrs[idx];
            uint8_t op = instr.op;
            if (is_jump_opcode(op)) {
                // An unresolved label is reported with a better message by
                // drain() above; just don't follow it here.
                if (auto* lbl = std::get_if<Label>(&instr.arg)) {
                    auto it = label_idx.find(lbl->id);
                    if (it != label_idx.end()) stack.push_back(it->second);
                }
                if (!is_unconditional_jump(op)) stack.push_back(idx + 1);
            } else if (is_stackdepth_neutral(op)) {
                size_t next_idx = idx + 1;
                if (next_idx < n && instrs[next_idx].op == POP_TOP_OPCODE)
                    next_idx += 1;
                stack.push_back(next_idx);
            } else if (!is_scope_exit(op)) {
                stack.push_back(idx + 1);
            }
        }
    }

    auto candidate_for_entry = [&](size_t start_idx, size_t stop_idx, bool& ok) -> int {
        ok = true;
        int candidate = std::numeric_limits<int>::max();
        for (size_t idx = start_idx; idx < stop_idx; ++idx) {
            int depth = visited[idx];
            if (depth == UNVISITED) continue;
            if (depth < candidate) candidate = depth;

            const Instr& instr = instrs[idx];
            uint8_t op = instr.op;
            int arg_val = 0;
            if (auto* iv = std::get_if<int>(&instr.arg)) arg_val = *iv;

            if (is_jump_opcode(op)) {
                int effect_taken;
                if (!stack_effect(op, arg_val, 1, effect_taken)) { ok = false; return 0; }
                size_t target_idx = n;  // sentinel: "outside the region"
                if (auto* lbl = std::get_if<Label>(&instr.arg)) {
                    auto it = label_idx.find(lbl->id);
                    if (it != label_idx.end()) target_idx = it->second;
                }
                if (target_idx < start_idx || target_idx >= stop_idx) {
                    int depth_taken = depth + effect_taken;
                    if (depth_taken < candidate) candidate = depth_taken;
                }
                if (!is_unconditional_jump(op)) {
                    int effect_fall;
                    if (!stack_effect(op, arg_val, 0, effect_fall)) { ok = false; return 0; }
                    size_t fall_idx = idx + 1;
                    if (fall_idx < start_idx || fall_idx >= stop_idx) {
                        int depth_fall = depth + effect_fall;
                        if (depth_fall < candidate) candidate = depth_fall;
                    }
                }
            } else {
                int effect;
                if (!stack_effect(op, arg_val, -1, effect)) { ok = false; return 0; }
                int depth_next = depth + effect;
                size_t fall_idx = idx + 1;
                if (is_scope_exit(op) || fall_idx >= stop_idx) {
                    if (depth_next < candidate) candidate = depth_next;
                }
            }
        }
        return candidate;
    };

    size_t m = exc_labeled.size();

    // Static grouping by handler — a property of the entry list itself, not
    // of traversal order.
    std::unordered_map<int, std::vector<size_t>> handler_groups;
    for (size_t i = 0; i < m; ++i)
        handler_groups[exc_labeled[i].handler_lbl.id].push_back(i);

    std::vector<bool> resolved(m, false);
    std::vector<bool> candidate_done(m, false);
    std::vector<int> candidate(m, 0);
    for (size_t i = 0; i < m; ++i)
        if (exc_labeled[i].depth != EXC_DEPTH_AUTO) { resolved[i] = true; candidate_done[i] = true; }

    for (;;) {
        bool progress = false;

        // Compute a candidate for every AUTO entry whose start is now
        // reachable.
        for (size_t i = 0; i < m; ++i) {
            if (candidate_done[i]) continue;
            ExcEntryL& e = exc_labeled[i];
            auto start_it = label_idx.find(e.start_lbl.id);
            if (start_it == label_idx.end()) {
                PyErr_SetString(PyExc_ValueError,
                    "cannot compute stack size: unresolved exception entry start label");
                return -1;
            }
            if (!normally_reachable[start_it->second]) {
                PyErr_SetString(PyExc_ValueError,
                    "cannot compute stack size: exception entry's protected region is "
                    "only reachable by unwinding into an exception handler, so the "
                    "depth CPython would record for it (the base depth of the "
                    "enclosing try block) is not recoverable from assembled "
                    "bytecode — pass an explicit ExcEntry.depth for this entry");
                return -1;
            }
            if (visited[start_it->second] == UNVISITED) continue;  // not reachable yet

            auto stop_it = label_idx.find(e.stop_lbl.id);
            if (stop_it == label_idx.end()) {
                PyErr_SetString(PyExc_ValueError,
                    "cannot compute stack size: unresolved exception entry stop label");
                return -1;
            }

            bool ok;
            candidate[i] = candidate_for_entry(start_it->second, stop_it->second, ok);
            if (!ok) return -1;
            candidate_done[i] = true;
            progress = true;
        }

        // Seed every handler group all of whose members now have a
        // candidate (fixed entries always do, from initialization above).
        for (auto& [handler_id, members] : handler_groups) {
            (void)handler_id;
            bool any_unresolved = false;
            bool all_ready = true;
            for (size_t i : members) {
                if (!resolved[i]) any_unresolved = true;
                if (!candidate_done[i]) all_ready = false;
            }
            if (!any_unresolved || !all_ready) continue;

            int group_min = std::numeric_limits<int>::max();
            for (size_t i : members) {
                int v = resolved[i] ? exc_labeled[i].depth : candidate[i];
                if (v < group_min) group_min = v;
            }
            for (size_t i : members) {
                if (resolved[i]) continue;
                exc_labeled[i].depth = group_min;
                if (!seed_handler(exc_labeled[i], group_min)) return -1;
                resolved[i] = true;
            }
            progress = true;
        }

        // Drain whenever this round made progress (a new candidate or a
        // freshly seeded handler) before deciding whether to stop: a
        // handler seeded just above still has its worklist entry
        // unprocessed, and its instructions' stack effects (e.g.
        // PUSH_EXC_INFO) haven't been folded into maxdepth yet. With no
        // AUTO entries at all (m == 0), progress is never set and this is
        // skipped — resolved is vacuously all-true below.
        if (progress) {
            if (!drain()) return -1;
        }
        if (std::all_of(resolved.begin(), resolved.end(), [](bool b) { return b; })) break;
        if (!progress) {
            PyErr_SetString(PyExc_ValueError,
                "cannot compute stack size: exception entry's protected region is "
                "unreachable from normal control flow — cannot infer its depth "
                "automatically (pass an explicit ExcEntry.depth instead)");
            return -1;
        }
    }
#endif

    return maxdepth;
}
