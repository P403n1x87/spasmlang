#include "bytecode.h"
#include "arg_kind_gen.h"
#include "opcode_names_gen.h"

#include <stdexcept>

// ════════════════════════════════════════════════════════════════════════════
// Forward declarations
// ════════════════════════════════════════════════════════════════════════════

extern PyTypeObject PyLabelType;
extern PyTypeObject PyInstrType;
extern PyTypeObject PyBytecodeType;

// ════════════════════════════════════════════════════════════════════════════
// Label type
// ════════════════════════════════════════════════════════════════════════════

struct PyLabelObject {
    PyObject_HEAD
    int id;
};

static PyObject* PyLabel_new(PyTypeObject* type, PyObject* args, PyObject* /*kw*/)
{
    int id;
    if (!PyArg_ParseTuple(args, "i", &id)) return nullptr;
    auto* self = reinterpret_cast<PyLabelObject*>(type->tp_alloc(type, 0));
    if (self) self->id = id;
    return reinterpret_cast<PyObject*>(self);
}

static PyObject* PyLabel_repr(PyLabelObject* self)
{
    return PyUnicode_FromFormat("<Label id=%d>", self->id);
}

static PyObject* PyLabel_get_id(PyLabelObject* self, void*)
{
    return PyLong_FromLong(self->id);
}

static PyGetSetDef PyLabel_getset[] = {
    {"id", (getter)PyLabel_get_id, nullptr, "label identifier", nullptr},
    {nullptr},
};

static PyObject* PyLabel_richcmp(PyObject* a, PyObject* b, int op)
{
    if (!PyObject_TypeCheck(a, &PyLabelType) || !PyObject_TypeCheck(b, &PyLabelType))
        Py_RETURN_NOTIMPLEMENTED;
    int ia = reinterpret_cast<PyLabelObject*>(a)->id;
    int ib = reinterpret_cast<PyLabelObject*>(b)->id;
    bool result = false;
    switch (op) {
    case Py_EQ: result = ia == ib; break;
    case Py_NE: result = ia != ib; break;
    default:    Py_RETURN_NOTIMPLEMENTED;
    }
    return PyBool_FromLong(result);
}

static Py_hash_t PyLabel_hash(PyLabelObject* self)
{
    return static_cast<Py_hash_t>(self->id);
}

PyTypeObject PyLabelType = {
    .ob_base      = PyVarObject_HEAD_INIT(nullptr, 0)
    .tp_name      = "spasm._core.Label",
    .tp_basicsize = sizeof(PyLabelObject),
    .tp_repr      = (reprfunc)PyLabel_repr,
    .tp_hash      = (hashfunc)PyLabel_hash,
    .tp_flags     = Py_TPFLAGS_DEFAULT,
    .tp_doc       = "Symbolic jump target.",
    .tp_richcompare = PyLabel_richcmp,
    .tp_getset    = PyLabel_getset,
    .tp_new       = PyLabel_new,
};

// ════════════════════════════════════════════════════════════════════════════
// Instr type
// ════════════════════════════════════════════════════════════════════════════

struct PyInstrObject {
    PyObject_HEAD
    uint8_t   op;
    PyObject* arg;      // owned: int | Label | arbitrary Python object
    int       lineno;
    int       end_lineno;
    int       col_offset;
    int       end_col;
    PyObject* labels;   // owned: list of Label — jump targets landing here
};

static void PyInstr_dealloc(PyInstrObject* self)
{
    Py_XDECREF(self->arg);
    Py_XDECREF(self->labels);
    Py_TYPE(self)->tp_free(self);
}

// Validate that v is a list of Label objects; return a new-reference copy,
// or NULL with a Python exception set. `field_name` is used in error messages.
static PyObject* check_labels_list(PyObject* v, const char* field_name = "labels")
{
    if (!v || !PyList_Check(v)) {
        PyErr_Format(PyExc_TypeError, "%s must be a list of Label", field_name);
        return nullptr;
    }
    Py_ssize_t n = PyList_GET_SIZE(v);
    for (Py_ssize_t i = 0; i < n; ++i) {
        if (!PyObject_TypeCheck(PyList_GET_ITEM(v, i), &PyLabelType)) {
            PyErr_Format(PyExc_TypeError,
                "%s[%zd] is not a Label (got %s)", field_name, i,
                Py_TYPE(PyList_GET_ITEM(v, i))->tp_name);
            return nullptr;
        }
    }
    Py_INCREF(v);
    return v;
}

// Resolve an opcode given either as an int (0..255) or an opname string
// (e.g. "LOAD_FAST", matching dis.opmap). Returns -1 with a Python
// exception set on failure.
static int resolve_opcode(PyObject* op_obj)
{
    int op;
    if (PyUnicode_Check(op_obj)) {
        const char* name = PyUnicode_AsUTF8(op_obj);
        if (!name) return -1;
        const auto& table = opcode_name_table();
        auto it = table.find(name);
        if (it == table.end()) {
            PyErr_Format(PyExc_ValueError, "unknown opcode name %R", op_obj);
            return -1;
        }
        op = it->second;
    } else {
        long v = PyLong_AsLong(op_obj);
        if (v == -1 && PyErr_Occurred()) return -1;
        op = static_cast<int>(v);
    }
    if (op < 0 || op > 255) {
        PyErr_SetString(PyExc_ValueError, "op must be in 0..255");
        return -1;
    }
    return op;
}

// Instr(op, arg=0, *, lineno=-1, end_lineno=-1, col_offset=-1, end_col=-1,
//       labels=())
// `op` may be an int (0..255) or an opname string (e.g. "LOAD_FAST").
// `labels` seeds the list of Labels targeting this instruction.
static int PyInstr_init(PyInstrObject* self, PyObject* args, PyObject* kw)
{
    static const char* kwlist[] = {
        "op", "arg", "lineno", "end_lineno", "col_offset", "end_col",
        "labels", nullptr
    };
    PyObject* op_obj = nullptr;
    PyObject* arg    = nullptr;
    PyObject* labels = nullptr;
    int lineno     = -1, end_lineno = -1, col_offset = -1, end_col = -1;

    if (!PyArg_ParseTupleAndKeywords(
            args, kw, "O|OiiiiO",
            const_cast<char**>(kwlist),
            &op_obj, &arg, &lineno, &end_lineno, &col_offset, &end_col, &labels))
        return -1;

    int op = resolve_opcode(op_obj);
    if (op < 0) return -1;

    PyObject* labels_list;
    if (labels == nullptr) {
        labels_list = PyList_New(0);
        if (!labels_list) return -1;
    } else {
        PyObject* seq = PySequence_List(labels);
        if (!seq) return -1;
        labels_list = check_labels_list(seq);
        Py_DECREF(seq);
        if (!labels_list) return -1;
    }

    Py_XDECREF(self->arg);
    if (arg == nullptr) {
        self->arg = PyLong_FromLong(0);
    } else {
        Py_INCREF(arg);
        self->arg = arg;
    }

    Py_XDECREF(self->labels);
    self->labels = labels_list;

    self->op          = static_cast<uint8_t>(op);
    self->lineno      = lineno;
    self->end_lineno  = end_lineno;
    self->col_offset  = col_offset;
    self->end_col     = end_col;
    return 0;
}

static PyObject* PyInstr_new(PyTypeObject* type, PyObject* /*args*/, PyObject* /*kw*/)
{
    auto* self = reinterpret_cast<PyInstrObject*>(type->tp_alloc(type, 0));
    if (!self) return nullptr;

    self->op         = 0;
    self->arg        = PyLong_FromLong(0);
    self->lineno     = -1;
    self->end_lineno = -1;
    self->col_offset = -1;
    self->end_col    = -1;
    self->labels     = PyList_New(0);

    if (!self->arg || !self->labels) {
        Py_DECREF(self);
        return nullptr;
    }
    return reinterpret_cast<PyObject*>(self);
}

static PyObject* PyInstr_repr(PyInstrObject* self)
{
    if (PyList_GET_SIZE(self->labels) > 0)
        return PyUnicode_FromFormat("Instr(%d, %R, labels=%R)",
                                     (int)self->op, self->arg, self->labels);
    return PyUnicode_FromFormat("Instr(%d, %R)", (int)self->op, self->arg);
}

// ── getset ────────────────────────────────────────────────────────────────

#define INSTR_INT_GETSET(field) \
    static PyObject* PyInstr_get_##field(PyInstrObject* s, void*) { return PyLong_FromLong(s->field); } \
    static int       PyInstr_set_##field(PyInstrObject* s, PyObject* v, void*) { \
        int n = static_cast<int>(PyLong_AsLong(v)); \
        if (n == -1 && PyErr_Occurred()) return -1; \
        s->field = n; return 0; }

INSTR_INT_GETSET(lineno)
INSTR_INT_GETSET(end_lineno)
INSTR_INT_GETSET(col_offset)
INSTR_INT_GETSET(end_col)

static PyObject* PyInstr_get_op(PyInstrObject* self, void*)
{
    return PyLong_FromLong(self->op);
}
static int PyInstr_set_op(PyInstrObject* self, PyObject* v, void*)
{
    if (!v) { PyErr_SetString(PyExc_TypeError, "cannot delete op"); return -1; }
    int op = resolve_opcode(v);
    if (op < 0) return -1;
    self->op = static_cast<uint8_t>(op);
    return 0;
}
static PyObject* PyInstr_get_arg(PyInstrObject* self, void*)
{
    Py_INCREF(self->arg);
    return self->arg;
}
static int PyInstr_set_arg(PyInstrObject* self, PyObject* v, void*)
{
    if (!v) { PyErr_SetString(PyExc_TypeError, "cannot delete arg"); return -1; }
    Py_INCREF(v);
    Py_DECREF(self->arg);
    self->arg = v;
    return 0;
}

static PyObject* PyInstr_get_labels(PyInstrObject* self, void*)
{
    Py_INCREF(self->labels);
    return self->labels;
}
static int PyInstr_set_labels(PyInstrObject* self, PyObject* v, void*)
{
    PyObject* checked = check_labels_list(v);
    if (!checked) return -1;
    Py_DECREF(self->labels);
    self->labels = checked;
    return 0;
}

static PyGetSetDef PyInstr_getset[] = {
    {"op",          (getter)PyInstr_get_op,          (setter)PyInstr_set_op,          "opcode byte (settable as an int or an opname string, e.g. \"LOAD_FAST\")", nullptr},
    {"arg",         (getter)PyInstr_get_arg,          (setter)PyInstr_set_arg,         "argument",        nullptr},
    {"lineno",      (getter)PyInstr_get_lineno,       (setter)PyInstr_set_lineno,      "line number",     nullptr},
    {"end_lineno",  (getter)PyInstr_get_end_lineno,   (setter)PyInstr_set_end_lineno,  "end line number", nullptr},
    {"col_offset",  (getter)PyInstr_get_col_offset,   (setter)PyInstr_set_col_offset,  "column offset",   nullptr},
    {"end_col",     (getter)PyInstr_get_end_col,      (setter)PyInstr_set_end_col,     "end column",      nullptr},
    {"labels",      (getter)PyInstr_get_labels,       (setter)PyInstr_set_labels,
     "List of Label objects that are jump targets landing on this instruction.", nullptr},
    {nullptr},
};

PyTypeObject PyInstrType = {
    .ob_base      = PyVarObject_HEAD_INIT(nullptr, 0)
    .tp_name      = "spasm._core.Instr",
    .tp_basicsize = sizeof(PyInstrObject),
    .tp_dealloc   = (destructor)PyInstr_dealloc,
    .tp_repr      = (reprfunc)PyInstr_repr,
    .tp_flags     = Py_TPFLAGS_DEFAULT,
    .tp_doc       = "A single bytecode instruction. op may be an int or an "
                    "opname string (e.g. \"LOAD_FAST\", matching dis.opmap). "
                    "labels is a list of Label objects that are jump targets "
                    "landing on this instruction.",
    .tp_getset    = PyInstr_getset,
    .tp_init      = (initproc)PyInstr_init,
    .tp_new       = PyInstr_new,
};

// ════════════════════════════════════════════════════════════════════════════
// Instr <-> C++ conversion helpers
// ════════════════════════════════════════════════════════════════════════════

// Create a PyInstrObject from a C++ Instr.
static PyObject* pyinstr_from_cpp(const Instr& ci)
{
    auto* obj = reinterpret_cast<PyInstrObject*>(
        PyInstrType.tp_alloc(&PyInstrType, 0));
    if (!obj) return nullptr;

    obj->op          = ci.op;
    obj->lineno      = ci.loc.lineno;
    obj->end_lineno  = ci.loc.end_lineno;
    obj->col_offset  = ci.loc.col_offset;
    obj->end_col     = ci.loc.end_col;

    if (auto* iv = std::get_if<int>(&ci.arg)) {
        obj->arg = PyLong_FromLong(*iv);
    } else if (auto* lv = std::get_if<Label>(&ci.arg)) {
        auto* lobj = reinterpret_cast<PyLabelObject*>(
            PyLabelType.tp_alloc(&PyLabelType, 0));
        if (!lobj) { Py_DECREF(obj); return nullptr; }
        lobj->id = lv->id;
        obj->arg = reinterpret_cast<PyObject*>(lobj);
    } else if (auto* pv = std::get_if<PyObject*>(&ci.arg)) {
        obj->arg = *pv;
        Py_INCREF(obj->arg);
    } else {
        obj->arg = PyLong_FromLong(0);
    }

    if (!obj->arg) { Py_DECREF(obj); return nullptr; }

    obj->labels = PyList_New(static_cast<Py_ssize_t>(ci.labels.size()));
    if (!obj->labels) { Py_DECREF(obj); return nullptr; }
    for (size_t i = 0; i < ci.labels.size(); ++i) {
        auto* lobj = reinterpret_cast<PyLabelObject*>(
            PyLabelType.tp_alloc(&PyLabelType, 0));
        if (!lobj) { Py_DECREF(obj); return nullptr; }
        lobj->id = ci.labels[i].id;
        PyList_SET_ITEM(obj->labels, static_cast<Py_ssize_t>(i),
                         reinterpret_cast<PyObject*>(lobj));
    }

    return reinterpret_cast<PyObject*>(obj);
}

// Convert a PyInstrObject back to a C++ Instr.
// Returns false and sets a Python exception on failure.
static bool pyinstr_to_cpp(PyInstrObject* obj, Instr& out)
{
    out.op = obj->op;
    out.loc = Location{obj->lineno, obj->end_lineno, obj->col_offset, obj->end_col};

    out.labels.clear();
    Py_ssize_t nlbl = PyList_GET_SIZE(obj->labels);
    out.labels.reserve(static_cast<size_t>(nlbl));
    for (Py_ssize_t i = 0; i < nlbl; ++i)
        out.labels.push_back(Label{
            reinterpret_cast<PyLabelObject*>(PyList_GET_ITEM(obj->labels, i))->id});

    if (PyObject_TypeCheck(obj->arg, &PyLabelType)) {
        out.arg = Label{reinterpret_cast<PyLabelObject*>(obj->arg)->id};
        return true;
    }

    ArgKind ak = arg_kind(obj->op);
    switch (ak) {
    case ArgKind::CONST:
        // Any Python object (including int) may be a constant value.
        out.arg = obj->arg;
        return true;
    case ArgKind::LOCAL:
    case ArgKind::FREE:
        // Abstract only when arg is a str; integers are raw opargs (packed ops
        // or out-of-bounds fallbacks — must be emitted as-is).
        if (PyUnicode_Check(obj->arg)) {
            out.arg = obj->arg;
            return true;
        }
        break;
    default:
        break;
    }

    if (PyLong_Check(obj->arg)) {
        long v = PyLong_AsLong(obj->arg);
        if (v == -1 && PyErr_Occurred()) return false;
        out.arg = static_cast<int>(v);
    } else {
        out.arg = obj->arg;
    }
    return true;
}

// ════════════════════════════════════════════════════════════════════════════
// ExcEntry type  (3.11+ only, but always compiled — just has no entries on <3.11)
// ════════════════════════════════════════════════════════════════════════════

extern PyTypeObject PyExcEntryType;

struct PyExcEntryObject {
    PyObject_HEAD
    PyObject* start;    // Label — inclusive start of try block
    PyObject* stop;     // Label — exclusive end of try block
    PyObject* handler;  // Label — exception handler target
    int       depth;    // stack depth at start of protected region, or
                         // EXC_DEPTH_AUTO for to_code() to compute it
    int       lasti;    // bool: push lasti
};

static void PyExcEntry_dealloc(PyExcEntryObject* self)
{
    Py_XDECREF(self->start);
    Py_XDECREF(self->stop);
    Py_XDECREF(self->handler);
    Py_TYPE(self)->tp_free(self);
}

// ExcEntry(start, stop, handler, depth=EXC_DEPTH_AUTO, lasti=False)
static int PyExcEntry_init(PyExcEntryObject* self, PyObject* args, PyObject* kw)
{
    static const char* kwlist[] = {"start", "stop", "handler", "depth", "lasti", nullptr};
    PyObject* start = nullptr, *stop = nullptr, *handler = nullptr;
    int depth = EXC_DEPTH_AUTO, lasti = 0;
    if (!PyArg_ParseTupleAndKeywords(args, kw, "O!O!O!|ii",
            const_cast<char**>(kwlist),
            &PyLabelType, &start, &PyLabelType, &stop, &PyLabelType, &handler,
            &depth, &lasti))
        return -1;
    Py_INCREF(start); Py_XDECREF(self->start); self->start = start;
    Py_INCREF(stop);  Py_XDECREF(self->stop);  self->stop  = stop;
    Py_INCREF(handler); Py_XDECREF(self->handler); self->handler = handler;
    self->depth = depth;
    self->lasti = lasti;
    return 0;
}

static PyObject* PyExcEntry_new(PyTypeObject* type, PyObject*, PyObject*)
{
    auto* self = reinterpret_cast<PyExcEntryObject*>(type->tp_alloc(type, 0));
    if (self) { self->start = self->stop = self->handler = nullptr;
                self->depth = EXC_DEPTH_AUTO; self->lasti = 0; }
    return reinterpret_cast<PyObject*>(self);
}

static PyObject* PyExcEntry_repr(PyExcEntryObject* self)
{
    return PyUnicode_FromFormat("ExcEntry(start=%R, stop=%R, handler=%R, depth=%d, lasti=%d)",
                                self->start, self->stop, self->handler,
                                self->depth, self->lasti);
}

#define EXCENTRY_OBJ_GETSET(field, doc)                                           \
    static PyObject* PyExcEntry_get_##field(PyExcEntryObject* s, void*) {         \
        Py_INCREF(s->field); return s->field; }                                   \
    static int PyExcEntry_set_##field(PyExcEntryObject* s, PyObject* v, void*) {  \
        if (!v || !PyObject_TypeCheck(v, &PyLabelType)) {                         \
            PyErr_SetString(PyExc_TypeError, #field " must be a Label"); return -1;} \
        Py_INCREF(v); Py_DECREF(s->field); s->field = v; return 0; }

EXCENTRY_OBJ_GETSET(start,   "Inclusive start Label.")
EXCENTRY_OBJ_GETSET(stop,    "Exclusive stop Label.")
EXCENTRY_OBJ_GETSET(handler, "Handler target Label.")
#undef EXCENTRY_OBJ_GETSET

static PyObject* PyExcEntry_get_depth(PyExcEntryObject* s, void*)
    { return PyLong_FromLong(s->depth); }
static int PyExcEntry_set_depth(PyExcEntryObject* s, PyObject* v, void*)
    { long n = PyLong_AsLong(v); if (n == -1 && PyErr_Occurred()) return -1;
      s->depth = static_cast<int>(n); return 0; }

static PyObject* PyExcEntry_get_lasti(PyExcEntryObject* s, void*)
    { return PyBool_FromLong(s->lasti); }
static int PyExcEntry_set_lasti(PyExcEntryObject* s, PyObject* v, void*)
    { s->lasti = PyObject_IsTrue(v); return 0; }

static PyGetSetDef PyExcEntry_getset[] = {
    {"start",   (getter)PyExcEntry_get_start,   (setter)PyExcEntry_set_start,   "inclusive start Label", nullptr},
    {"stop",    (getter)PyExcEntry_get_stop,     (setter)PyExcEntry_set_stop,    "exclusive stop Label",  nullptr},
    {"handler", (getter)PyExcEntry_get_handler,  (setter)PyExcEntry_set_handler, "handler Label",         nullptr},
    {"depth",   (getter)PyExcEntry_get_depth,     (setter)PyExcEntry_set_depth,   "stack depth",           nullptr},
    {"lasti",   (getter)PyExcEntry_get_lasti,    (setter)PyExcEntry_set_lasti,   "push lasti",            nullptr},
    {nullptr},
};

PyTypeObject PyExcEntryType = {
    .ob_base      = PyVarObject_HEAD_INIT(nullptr, 0)
    .tp_name      = "spasm._core.ExcEntry",
    .tp_basicsize = sizeof(PyExcEntryObject),
    .tp_dealloc   = (destructor)PyExcEntry_dealloc,
    .tp_repr      = (reprfunc)PyExcEntry_repr,
    .tp_flags     = Py_TPFLAGS_DEFAULT,
    .tp_doc       = "One exception table entry (labels, depth, lasti).",
    .tp_getset    = PyExcEntry_getset,
    .tp_init      = (initproc)PyExcEntry_init,
    .tp_new       = PyExcEntry_new,
};

// ════════════════════════════════════════════════════════════════════════════
// Bytecode type
// ════════════════════════════════════════════════════════════════════════════

struct PyBytecodeObject {
    PyObject_HEAD
    Bytecode* bc;            // owns CodeMeta + exc_labeled; instrs synced on demand
    PyObject* py_instrs;     // Python list of PyInstrObject — canonical instruction store
    PyObject* py_exc_entries; // Python list of PyExcEntryObject
    PyObject* py_end_labels; // Python list of Label — targets one-past-the-last-instruction
};

static void PyBytecode_dealloc(PyBytecodeObject* self)
{
    Py_XDECREF(self->py_instrs);
    Py_XDECREF(self->py_exc_entries);
    Py_XDECREF(self->py_end_labels);
    delete self->bc;
    Py_TYPE(self)->tp_free(self);
}

// ── construction from scratch ──────────────────────────────────────────────
// Bytecode() builds an empty template: no instructions, empty consts/names/
// varnames/freevars/cellvars, argcount=0, flags=0, firstlineno=1,
// filename="<string>", name/qualname="<bytecode>". Callers fill in .instrs
// (optionally via the constructor's `instrs` argument) and whichever
// metadata their code needs before calling .to_code(). co_stacksize is
// always computed automatically by .to_code() — there's no property for it.

static PyObject* PyBytecode_new(PyTypeObject* type, PyObject* /*args*/, PyObject* /*kw*/)
{
    auto* self = reinterpret_cast<PyBytecodeObject*>(type->tp_alloc(type, 0));
    if (!self) return nullptr;

    self->bc = new Bytecode();
    self->bc->meta.firstlineno = 1;
    self->bc->meta.consts   = PyList_New(0);
    self->bc->meta.names    = PyList_New(0);
    self->bc->meta.varnames = PyList_New(0);
    self->bc->meta.freevars = PyList_New(0);
    self->bc->meta.cellvars = PyList_New(0);
    self->bc->meta.filename = PyUnicode_FromString("<string>");
    self->bc->meta.name     = PyUnicode_FromString("<bytecode>");
    Py_XINCREF(self->bc->meta.name);
    self->bc->meta.qualname = self->bc->meta.name;

    self->py_instrs      = PyList_New(0);
    self->py_exc_entries = PyList_New(0);
    self->py_end_labels  = PyList_New(0);

    if (!self->bc->meta.consts || !self->bc->meta.names || !self->bc->meta.varnames ||
        !self->bc->meta.freevars || !self->bc->meta.cellvars ||
        !self->bc->meta.filename || !self->bc->meta.name ||
        !self->py_instrs || !self->py_exc_entries || !self->py_end_labels) {
        Py_DECREF(self);
        return nullptr;
    }

    return reinterpret_cast<PyObject*>(self);
}

// Bytecode(instrs=())
static int PyBytecode_init(PyBytecodeObject* self, PyObject* args, PyObject* kw)
{
    static const char* kwlist[] = {"instrs", nullptr};
    PyObject* instrs = nullptr;
    if (!PyArg_ParseTupleAndKeywords(args, kw, "|O", const_cast<char**>(kwlist), &instrs))
        return -1;
    if (instrs == nullptr) return 0;

    PyObject* lst = PySequence_List(instrs);
    if (!lst) return -1;

    Py_ssize_t n = PyList_GET_SIZE(lst);
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* item = PyList_GET_ITEM(lst, i);
        if (!PyObject_TypeCheck(item, &PyInstrType)) {
            PyErr_Format(PyExc_TypeError,
                "instrs[%zd] is not an Instr (got %s)", i, Py_TYPE(item)->tp_name);
            Py_DECREF(lst);
            return -1;
        }
    }

    Py_DECREF(self->py_instrs);
    self->py_instrs = lst;
    return 0;
}

// ── from_code ─────────────────────────────────────────────────────────────

static PyObject* PyBytecode_from_code(PyObject* /*cls*/, PyObject* args)
{
    PyObject* code_obj;
    if (!PyArg_ParseTuple(args, "O!", &PyCode_Type, &code_obj))
        return nullptr;

    auto* self = reinterpret_cast<PyBytecodeObject*>(
        PyBytecodeType.tp_alloc(&PyBytecodeType, 0));
    if (!self) return nullptr;

    try {
        self->bc = new Bytecode(
            Bytecode::from_code(reinterpret_cast<PyCodeObject*>(code_obj)));
    } catch (const std::exception& e) {
        PyErr_SetString(PyExc_RuntimeError, e.what());
        Py_DECREF(self);
        return nullptr;
    }

    // Convert C++ instrs → Python list of PyInstrObject.
    self->py_instrs = PyList_New(
        static_cast<Py_ssize_t>(self->bc->instrs.size()));
    if (!self->py_instrs) { Py_DECREF(self); return nullptr; }

    for (size_t i = 0; i < self->bc->instrs.size(); ++i) {
        PyObject* pi = pyinstr_from_cpp(self->bc->instrs[i]);
        if (!pi) { Py_DECREF(self); return nullptr; }
        PyList_SET_ITEM(self->py_instrs, static_cast<Py_ssize_t>(i), pi);
    }

    // C++ instrs no longer needed; py_instrs is canonical from here.
    self->bc->instrs.clear();
    self->bc->instrs.shrink_to_fit();

    // Convert end_labels → Python list of Label.
    self->py_end_labels = PyList_New(
        static_cast<Py_ssize_t>(self->bc->end_labels.size()));
    if (!self->py_end_labels) { Py_DECREF(self); return nullptr; }
    for (size_t i = 0; i < self->bc->end_labels.size(); ++i) {
        auto* lobj = reinterpret_cast<PyLabelObject*>(
            PyLabelType.tp_alloc(&PyLabelType, 0));
        if (!lobj) { Py_DECREF(self); return nullptr; }
        lobj->id = self->bc->end_labels[i].id;
        PyList_SET_ITEM(self->py_end_labels, static_cast<Py_ssize_t>(i),
                         reinterpret_cast<PyObject*>(lobj));
    }

    // Convert exc_labeled → Python list of PyExcEntryObject. Its Label
    // fields were already attached to the matching py_instrs entries (or
    // py_end_labels) above, via bc->instrs[i].labels / bc->end_labels.
#if HAS_EXCEPTION_TABLE
    self->py_exc_entries = PyList_New(
        static_cast<Py_ssize_t>(self->bc->exc_labeled.size()));
    if (!self->py_exc_entries) { Py_DECREF(self); return nullptr; }

    for (size_t i = 0; i < self->bc->exc_labeled.size(); ++i) {
        const ExcEntryL& el = self->bc->exc_labeled[i];

        auto make_label = [](int id) -> PyObject* {
            auto* lobj = reinterpret_cast<PyLabelObject*>(
                PyLabelType.tp_alloc(&PyLabelType, 0));
            if (lobj) lobj->id = id;
            return reinterpret_cast<PyObject*>(lobj);
        };

        PyObject* start   = make_label(el.start_lbl.id);
        PyObject* stop    = make_label(el.stop_lbl.id);
        PyObject* handler = make_label(el.handler_lbl.id);
        if (!start || !stop || !handler) {
            Py_XDECREF(start); Py_XDECREF(stop); Py_XDECREF(handler);
            Py_DECREF(self); return nullptr;
        }

        auto* ee = reinterpret_cast<PyExcEntryObject*>(
            PyExcEntryType.tp_alloc(&PyExcEntryType, 0));
        if (!ee) {
            Py_DECREF(start); Py_DECREF(stop); Py_DECREF(handler);
            Py_DECREF(self); return nullptr;
        }
        ee->start   = start;
        ee->stop    = stop;
        ee->handler = handler;
        ee->depth   = el.depth;
        ee->lasti   = el.lasti ? 1 : 0;
        PyList_SET_ITEM(self->py_exc_entries,
                        static_cast<Py_ssize_t>(i),
                        reinterpret_cast<PyObject*>(ee));
    }
#else
    self->py_exc_entries = PyList_New(0);
    if (!self->py_exc_entries) { Py_DECREF(self); return nullptr; }
#endif

    return reinterpret_cast<PyObject*>(self);
}

// ── to_code ───────────────────────────────────────────────────────────────

static PyObject* PyBytecode_to_code(PyBytecodeObject* self, PyObject*)
{
    if (!PyList_Check(self->py_instrs)) {
        PyErr_SetString(PyExc_TypeError, "instrs must be a list");
        return nullptr;
    }

    // Sync py_instrs → bc->instrs for assembly.
    Py_ssize_t n = PyList_GET_SIZE(self->py_instrs);
    self->bc->instrs.clear();
    self->bc->instrs.reserve(static_cast<size_t>(n));

    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* item = PyList_GET_ITEM(self->py_instrs, i);
        if (!PyObject_TypeCheck(item, &PyInstrType)) {
            PyErr_Format(PyExc_TypeError,
                "instrs[%zd] is not an Instr (got %s)", i,
                Py_TYPE(item)->tp_name);
            self->bc->instrs.clear();
            return nullptr;
        }
        Instr ci(0);
        if (!pyinstr_to_cpp(reinterpret_cast<PyInstrObject*>(item), ci)) {
            self->bc->instrs.clear();
            return nullptr;
        }
        self->bc->instrs.push_back(ci);
    }

    // Sync py_end_labels → bc->end_labels for assembly.
    if (!PyList_Check(self->py_end_labels)) {
        PyErr_SetString(PyExc_TypeError, "end_labels must be a list");
        self->bc->instrs.clear();
        return nullptr;
    }
    self->bc->end_labels.clear();
    Py_ssize_t nel = PyList_GET_SIZE(self->py_end_labels);
    self->bc->end_labels.reserve(static_cast<size_t>(nel));
    for (Py_ssize_t i = 0; i < nel; ++i) {
        PyObject* item = PyList_GET_ITEM(self->py_end_labels, i);
        if (!PyObject_TypeCheck(item, &PyLabelType)) {
            PyErr_Format(PyExc_TypeError,
                "end_labels[%zd] is not a Label (got %s)", i,
                Py_TYPE(item)->tp_name);
            self->bc->instrs.clear();
            return nullptr;
        }
        self->bc->end_labels.push_back(
            Label{reinterpret_cast<PyLabelObject*>(item)->id});
    }

    // Sync py_exc_entries → bc->exc_labeled for assembly.
#if HAS_EXCEPTION_TABLE
    self->bc->exc_labeled.clear();
    Py_ssize_t ne = PyList_GET_SIZE(self->py_exc_entries);
    self->bc->exc_labeled.reserve(static_cast<size_t>(ne));
    for (Py_ssize_t i = 0; i < ne; ++i) {
        PyObject* item = PyList_GET_ITEM(self->py_exc_entries, i);
        if (!PyObject_TypeCheck(item, &PyExcEntryType)) {
            PyErr_Format(PyExc_TypeError,
                "exc_entries[%zd] is not an ExcEntry (got %s)", i,
                Py_TYPE(item)->tp_name);
            self->bc->instrs.clear();
            return nullptr;
        }
        auto* ee = reinterpret_cast<PyExcEntryObject*>(item);
        ExcEntryL el;
        el.start_lbl   = Label{reinterpret_cast<PyLabelObject*>(ee->start)->id};
        el.stop_lbl    = Label{reinterpret_cast<PyLabelObject*>(ee->stop)->id};
        el.handler_lbl = Label{reinterpret_cast<PyLabelObject*>(ee->handler)->id};
        el.depth = ee->depth;
        el.lasti = ee->lasti != 0;
        self->bc->exc_labeled.push_back(el);
    }
#endif

    PyObject* result = self->bc->to_code();
    self->bc->instrs.clear();
    self->bc->end_labels.clear();
    return result;
}

// ── instrs property ───────────────────────────────────────────────────────

static PyObject* PyBytecode_get_instrs(PyBytecodeObject* self, void*)
{
    Py_INCREF(self->py_instrs);
    return self->py_instrs;
}

// ── exc_entries property ──────────────────────────────────────────────────

static PyObject* PyBytecode_get_exc_entries(PyBytecodeObject* self, void*)
{
    Py_INCREF(self->py_exc_entries);
    return self->py_exc_entries;
}

static int PyBytecode_set_exc_entries(PyBytecodeObject* self, PyObject* v, void*)
{
    if (!v || !PyList_Check(v)) {
        PyErr_SetString(PyExc_TypeError, "exc_entries must be a list");
        return -1;
    }
    Py_INCREF(v);
    Py_DECREF(self->py_exc_entries);
    self->py_exc_entries = v;
    return 0;
}

static int PyBytecode_set_instrs(PyBytecodeObject* self, PyObject* v, void*)
{
    if (!v || !PyList_Check(v)) {
        PyErr_SetString(PyExc_TypeError, "instrs must be a list");
        return -1;
    }
    Py_INCREF(v);
    Py_DECREF(self->py_instrs);
    self->py_instrs = v;
    return 0;
}

// ── end_labels property ───────────────────────────────────────────────────
// Labels targeting the position one-past-the-last-instruction — e.g. an
// exception table range that runs to the very end of the code with nothing
// after it. There's no instruction to attach these to, hence the separate
// list (mirrors Bytecode::end_labels on the C++ side).

static PyObject* PyBytecode_get_end_labels(PyBytecodeObject* self, void*)
{
    Py_INCREF(self->py_end_labels);
    return self->py_end_labels;
}
static int PyBytecode_set_end_labels(PyBytecodeObject* self, PyObject* v, void*)
{
    PyObject* checked = check_labels_list(v, "end_labels");
    if (!checked) return -1;
    Py_DECREF(self->py_end_labels);
    self->py_end_labels = checked;
    return 0;
}

// ── new_label ─────────────────────────────────────────────────────────────

static PyObject* PyBytecode_new_label(PyBytecodeObject* self, PyObject*)
{
    Label lbl = self->bc->new_label();
    auto* obj = reinterpret_cast<PyLabelObject*>(
        PyLabelType.tp_alloc(&PyLabelType, 0));
    if (!obj) return nullptr;
    obj->id = lbl.id;
    return reinterpret_cast<PyObject*>(obj);
}

// ── label_positions ───────────────────────────────────────────────────────
// Recompute label -> current index in .instrs in one O(n) pass, by scanning
// each instruction's .labels. Labels in .end_labels map to len(.instrs)
// (one past the last instruction). Jump targets are already Labels by the
// time a Bytecode reaches Python code (from_code() resolves them during
// decode), so this is purely an inspection/lookup helper for code that
// needs to know where a label currently points after edits — there is no
// separate "symbolify" step to run first.

static PyObject* PyBytecode_label_positions(PyBytecodeObject* self, PyObject*)
{
    if (!PyList_Check(self->py_instrs)) {
        PyErr_SetString(PyExc_TypeError, "instrs must be a list");
        return nullptr;
    }

    PyObject* result = PyDict_New();
    if (!result) return nullptr;

    Py_ssize_t n = PyList_GET_SIZE(self->py_instrs);
    for (Py_ssize_t i = 0; i < n; ++i) {
        auto* pi = reinterpret_cast<PyInstrObject*>(PyList_GET_ITEM(self->py_instrs, i));
        if (!PyList_Check(pi->labels)) continue;
        Py_ssize_t nl = PyList_GET_SIZE(pi->labels);
        for (Py_ssize_t j = 0; j < nl; ++j) {
            PyObject* lbl = PyList_GET_ITEM(pi->labels, j);
            PyObject* idx = PyLong_FromSsize_t(i);
            if (!idx || PyDict_SetItem(result, lbl, idx) < 0) {
                Py_XDECREF(idx);
                Py_DECREF(result);
                return nullptr;
            }
            Py_DECREF(idx);
        }
    }

    if (PyList_Check(self->py_end_labels)) {
        Py_ssize_t ne = PyList_GET_SIZE(self->py_end_labels);
        if (ne > 0) {
            PyObject* idx = PyLong_FromSsize_t(n);
            if (!idx) { Py_DECREF(result); return nullptr; }
            for (Py_ssize_t j = 0; j < ne; ++j) {
                PyObject* lbl = PyList_GET_ITEM(self->py_end_labels, j);
                if (PyDict_SetItem(result, lbl, idx) < 0) {
                    Py_DECREF(idx);
                    Py_DECREF(result);
                    return nullptr;
                }
            }
            Py_DECREF(idx);
        }
    }

    return result;
}

// ── Table property helpers ────────────────────────────────────────────────

#define TABLE_PROP(field, doc)                                                  \
    static PyObject* PyBytecode_get_##field(PyBytecodeObject* s, void*) {       \
        Py_INCREF(s->bc->meta.field); return s->bc->meta.field; }               \
    static int PyBytecode_set_##field(PyBytecodeObject* s, PyObject* v, void*) {\
        if (!v || !PyList_Check(v)) {                                            \
            PyErr_SetString(PyExc_TypeError, #field " must be a list"); return -1;} \
        Py_INCREF(v); Py_DECREF(s->bc->meta.field); s->bc->meta.field = v; return 0;}

TABLE_PROP(consts,   "Mutable list of co_consts entries.")
TABLE_PROP(names,    "Mutable list of co_names strings.")
TABLE_PROP(varnames, "Mutable list of co_varnames strings.")
TABLE_PROP(freevars, "Mutable list of co_freevars strings.")
TABLE_PROP(cellvars, "Mutable list of co_cellvars strings.")
#undef TABLE_PROP

// bc.filename, bc.name, bc.qualname (read/write str scalars)
#define STR_PROP(field, doc)                                                    \
    static PyObject* PyBytecode_get_##field(PyBytecodeObject* s, void*) {       \
        Py_INCREF(s->bc->meta.field); return s->bc->meta.field; }               \
    static int PyBytecode_set_##field(PyBytecodeObject* s, PyObject* v, void*) {\
        if (!v || !PyUnicode_Check(v)) {                                         \
            PyErr_SetString(PyExc_TypeError, #field " must be a str"); return -1;} \
        Py_INCREF(v); Py_DECREF(s->bc->meta.field); s->bc->meta.field = v; return 0;}

STR_PROP(filename, "Source filename (co_filename).")
STR_PROP(name,     "Code object name (co_name).")
STR_PROP(qualname, "Qualified name (co_qualname). Ignored on 3.10, which has no co_qualname.")
#undef STR_PROP

// bc.argcount, bc.flags, bc.firstlineno (read/write int scalars)
// Note: no stacksize property — co_stacksize is always computed
// automatically by .to_code() (see stackdepth.h), never user-settable.
#define INT_PROP(field, doc)                                                    \
    static PyObject* PyBytecode_get_##field(PyBytecodeObject* s, void*) {       \
        return PyLong_FromLong(s->bc->meta.field); }                            \
    static int PyBytecode_set_##field(PyBytecodeObject* s, PyObject* v, void*){\
        long n = PyLong_AsLong(v);                                              \
        if (n == -1 && PyErr_Occurred()) return -1;                             \
        s->bc->meta.field = static_cast<int>(n); return 0; }

INT_PROP(argcount,   "Number of positional arguments.")
INT_PROP(flags,      "Code flags.")
INT_PROP(firstlineno,"First line number.")
#undef INT_PROP

// ── add_const / add_name / add_varname ────────────────────────────────────

// find_or_add: search list by identity then equality; append if absent.
static Py_ssize_t py_find_or_add(PyObject* lst, PyObject* obj)
{
    Py_ssize_t n = PyList_GET_SIZE(lst);
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject* it = PyList_GET_ITEM(lst, i);
        if (it == obj) return i;
        int eq = PyObject_RichCompareBool(it, obj, Py_EQ);
        if (eq < 0) { PyErr_Clear(); continue; }
        if (eq) return i;
    }
    if (PyList_Append(lst, obj) < 0) return -1;
    return n;
}

static PyObject* PyBytecode_add_const(PyBytecodeObject* self, PyObject* obj)
{
    Py_ssize_t idx = py_find_or_add(self->bc->meta.consts, obj);
    if (idx < 0) return nullptr;
    return PyLong_FromSsize_t(idx);
}

static PyObject* PyBytecode_add_name(PyBytecodeObject* self, PyObject* obj)
{
    if (!PyUnicode_Check(obj)) {
        PyErr_SetString(PyExc_TypeError, "name must be a str");
        return nullptr;
    }
    Py_ssize_t idx = py_find_or_add(self->bc->meta.names, obj);
    if (idx < 0) return nullptr;
    return PyLong_FromSsize_t(idx);
}

static PyObject* PyBytecode_add_varname(PyBytecodeObject* self, PyObject* obj)
{
    if (!PyUnicode_Check(obj)) {
        PyErr_SetString(PyExc_TypeError, "varname must be a str");
        return nullptr;
    }
    Py_ssize_t idx = py_find_or_add(self->bc->meta.varnames, obj);
    if (idx < 0) return nullptr;
    return PyLong_FromSsize_t(idx);
}

// ── properties / methods ──────────────────────────────────────────────────

static PyGetSetDef PyBytecode_getset[] = {
    {"exc_entries", (getter)PyBytecode_get_exc_entries,  (setter)PyBytecode_set_exc_entries,
     "Mutable list of ExcEntry objects (exception table).", nullptr},
    {"instrs",      (getter)PyBytecode_get_instrs,      (setter)PyBytecode_set_instrs,
     "Mutable list of Instr objects.", nullptr},
    {"consts",      (getter)PyBytecode_get_consts,      (setter)PyBytecode_set_consts,
     "Mutable list of co_consts entries.", nullptr},
    {"names",       (getter)PyBytecode_get_names,       (setter)PyBytecode_set_names,
     "Mutable list of co_names strings.", nullptr},
    {"varnames",    (getter)PyBytecode_get_varnames,    (setter)PyBytecode_set_varnames,
     "Mutable list of co_varnames strings.", nullptr},
    {"freevars",    (getter)PyBytecode_get_freevars,    (setter)PyBytecode_set_freevars,
     "Mutable list of co_freevars strings.", nullptr},
    {"cellvars",    (getter)PyBytecode_get_cellvars,    (setter)PyBytecode_set_cellvars,
     "Mutable list of co_cellvars strings.", nullptr},
    {"filename",    (getter)PyBytecode_get_filename,    (setter)PyBytecode_set_filename,
     "Source filename (co_filename).", nullptr},
    {"name",        (getter)PyBytecode_get_name,        (setter)PyBytecode_set_name,
     "Code object name (co_name).", nullptr},
    {"qualname",    (getter)PyBytecode_get_qualname,    (setter)PyBytecode_set_qualname,
     "Qualified name (co_qualname). Ignored on 3.10.", nullptr},
    {"argcount",    (getter)PyBytecode_get_argcount,    (setter)PyBytecode_set_argcount,
     "Number of positional arguments.", nullptr},
    {"flags",       (getter)PyBytecode_get_flags,       (setter)PyBytecode_set_flags,
     "Code flags.", nullptr},
    {"firstlineno", (getter)PyBytecode_get_firstlineno, (setter)PyBytecode_set_firstlineno,
     "First line number.", nullptr},
    {"end_labels",  (getter)PyBytecode_get_end_labels,  (setter)PyBytecode_set_end_labels,
     "List of Label objects targeting the position one-past-the-last-instruction "
     "(e.g. an exception table range that runs to the very end of the code).", nullptr},
    {nullptr},
};

static PyMethodDef PyBytecode_methods[] = {
    {"from_code",         PyBytecode_from_code,                      METH_VARARGS | METH_CLASS,
     "Create a Bytecode from a code object. Jump targets are already "
     "resolved to Labels."},
    {"to_code",           (PyCFunction)PyBytecode_to_code,           METH_NOARGS,
     "Assemble back into a code object."},
    {"new_label",         (PyCFunction)PyBytecode_new_label,         METH_NOARGS,
     "Allocate and return a new Label."},
    {"label_positions",   (PyCFunction)PyBytecode_label_positions,   METH_NOARGS,
     "label_positions() -> dict[Label, int]: recompute where every label "
     "currently points, as an index into .instrs (or len(.instrs) for a "
     "label in .end_labels)."},
    {"add_const",         (PyCFunction)PyBytecode_add_const,         METH_O,
     "add_const(obj) -> int: find or append obj in co_consts, return its index."},
    {"add_name",          (PyCFunction)PyBytecode_add_name,          METH_O,
     "add_name(name) -> int: find or append name in co_names, return its index."},
    {"add_varname",       (PyCFunction)PyBytecode_add_varname,       METH_O,
     "add_varname(name) -> int: find or append name in co_varnames, return index."},
    {nullptr},
};

PyTypeObject PyBytecodeType = {
    .ob_base      = PyVarObject_HEAD_INIT(nullptr, 0)
    .tp_name      = "spasm._core.Bytecode",
    .tp_basicsize = sizeof(PyBytecodeObject),
    .tp_dealloc   = (destructor)PyBytecode_dealloc,
    .tp_flags     = Py_TPFLAGS_DEFAULT,
    .tp_doc       = "Mutable bytecode sequence. Bytecode(instrs=()) builds an "
                    "empty template for assembling from scratch; see also "
                    "Bytecode.from_code(). Jump targets are always Labels "
                    "(from_code() resolves them during decode) attached "
                    "directly to their target Instr's .labels, or to "
                    ".end_labels for the one-past-the-end position.",
    .tp_methods   = PyBytecode_methods,
    .tp_getset    = PyBytecode_getset,
    .tp_init      = (initproc)PyBytecode_init,
    .tp_new       = PyBytecode_new,
};

// ════════════════════════════════════════════════════════════════════════════
// Module
// ════════════════════════════════════════════════════════════════════════════

static PyModuleDef moduledef = {
    .m_base = PyModuleDef_HEAD_INIT,
    .m_name = "spasm._core",
    .m_doc  = "Native CPython bytecode manipulation.",
    .m_size = -1,
};

PyMODINIT_FUNC PyInit__core(void)
{
    if (PyType_Ready(&PyLabelType)    < 0) return nullptr;
    if (PyType_Ready(&PyInstrType)    < 0) return nullptr;
    if (PyType_Ready(&PyExcEntryType) < 0) return nullptr;
    if (PyType_Ready(&PyBytecodeType) < 0) return nullptr;

    PyObject* m = PyModule_Create(&moduledef);
    if (!m) return nullptr;

    auto add = [&](const char* name, PyTypeObject* tp) {
        Py_INCREF(tp);
        if (PyModule_AddObject(m, name, reinterpret_cast<PyObject*>(tp)) < 0) {
            Py_DECREF(tp);
            Py_DECREF(m);
            m = nullptr;
        }
    };

    add("Label",    &PyLabelType);
    add("Instr",    &PyInstrType);
    add("ExcEntry", &PyExcEntryType);
    add("Bytecode", &PyBytecodeType);
    if (!m) return nullptr;

    PyModule_AddIntConstant(m, "PY_VERSION_HEX", PY_VERSION_HEX);
    return m;
}
