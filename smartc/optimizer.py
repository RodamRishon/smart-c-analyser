"""
Code Optimizer
--------------
Classic optimisation passes over the three-address IR, run to a fixed
point (see `optimize` at the bottom). Every pass is written to preserve
the program's behaviour, so each one consults a small type model
(`OptContext`) before it rewrites arithmetic:

1. Constant folding and propagation -- expressions whose operands are
   known at compile time are evaluated once, using C's rules for the
   operand types (truncating integer division, int overflow left to the
   compiler, a constant stored into a variable converted to that
   variable's type). Which variables hold a known constant is found with
   a forward data-flow analysis over the control-flow graph, so values
   flow across branches and loops. A branch whose condition becomes
   constant is resolved.
2. Algebraic simplification -- identities that hold for the operand's
   type (`x + 0`, `x * 1`, `x * 0`, ...). Identities that are not exact
   for floating point are detected and reported but not applied.
3. Common subexpression elimination (CSE) -- when the same expression is
   computed twice with the same operand values inside one basic block,
   the second computation reuses the first result.
4. Copy propagation -- a copy is replaced by its source when both have
   the same type.
5. Dead code elimination (DCE) -- assignments whose results are never
   read, code that cannot be reached, and variables nothing uses.

Basic blocks are the unit for CSE and copy propagation; constant
propagation and dead-store elimination use iterative data-flow analyses
over the whole function's CFG.
"""

import math
import re
from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional, Tuple

from .ir import Instr

NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")
INDEXED_RE = re.compile(r"^([A-Za-z_]\w*)\[(.+)\]$")
SIZEOF_RE = re.compile(r"^sizeof\((.*)\)$")
# Rightmost '.'/'->' before a trailing member name -- peels one level
# of struct/union member access at a time (`p->next->val` -> base
# `p->next`, member `val`), so recursing on the base handles chains.
MEMBER_RE = re.compile(r"^(.*)(->|\.)([A-Za-z_]\w*)$")
IDENT_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")
TYPE_KEYWORD_BASES = {"int", "float", "char", "double", "void"}
COMMUTATIVE_OPS = {"+", "*", "==", "!=", "&&", "||"}


TEMP_RE = re.compile(r"^t\d+$")


def _is_temp(name) -> bool:
    return isinstance(name, str) and bool(TEMP_RE.match(name))


def _split_indexed(s: str) -> Optional[Tuple[str, List[str]]]:
    """'a[i]' -> ('a', ['i']); 'a[i][j]' -> ('a', ['i', 'j']) -- any
    number of dimensions. None if `s` isn't a (possibly multi-dim)
    array-indexed form at all (e.g. it has a '.'/'->' before the first
    '[', so it's a member access instead)."""
    m = re.match(r"^([A-Za-z_]\w*)(\[.*\])$", s)
    if not m:
        return None
    name, rest = m.group(1), m.group(2)
    indices: List[str] = []
    i, n = 0, len(rest)
    while i < n:
        if rest[i] != "[":
            return None
        depth = 1
        j = i + 1
        while j < n and depth > 0:
            if rest[j] == "[":
                depth += 1
            elif rest[j] == "]":
                depth -= 1
            j += 1
        if depth != 0:
            return None
        indices.append(rest[i + 1:j - 1])
        i = j
    return (name, indices) if indices else None


def _is_unsafe_write(dest) -> bool:
    """True for a write target that isn't a plain name or a pure
    array-element store (`a[i]`, `a[i][j]`, ...): a pointer
    dereference (`*p`), a struct/union member access (`s.f`, `p->f`),
    or any combination of these with indexing (`p->arr[i]`). These may
    alias memory this module has no name for, so every pass that sees
    one falls back to its fully conservative behaviour (drop every
    cached value / alias) instead of the narrower per-name
    invalidation a plain `a[i]` store gets."""
    if not isinstance(dest, str):
        return False
    if dest.startswith("*"):
        return True
    if "." in dest or "->" in dest:
        return True
    return False


def _base_name(name: str) -> str:
    """'a[i]' / 'a[i][j]' -> 'a'; plain names are returned unchanged."""
    split = _split_indexed(name)
    return split[0] if split else name


def _display(name):
    """User-facing form of an IR name (strips the alpha-rename suffix)."""
    return re.sub(r"__\d+", "", str(name))


EVENT_LABELS = {
    "fold": "Constant Folding",
    "algebraic": "Algebraic Simplification",
    "cse": "Common Sub-expression Elimination",
    "copy": "Copy Propagation",
    "inline": "Temporary Elimination",
    "dead-store": "Dead Code Elimination",
    "unreachable": "Dead Code Elimination",
    "pointer": "Pointer/Array Simplification",
}

# Optimisations a compiler textbook would list that this tool does NOT
# implement. Shown in the report so the output never over-claims.
UNSUPPORTED_OPTIMISATIONS = [
    "Loop-invariant code motion",
    "Strength reduction (e.g. x * 2 -> x << 1)",
    "Loop unrolling and loop fusion",
    "Function inlining and interprocedural constant propagation",
    "Common subexpression elimination across basic blocks (global value numbering)",
    "Removal of unused functions",
    "Re-structuring goto/label control flow back into if/while/for",
    "Scalar replacement of arrays and structs",
]


@dataclass
class OptEvent:
    line: int
    kind: str      # "fold" | "algebraic" | "cse" | "dead-store" | "unreachable" | "copy"
    message: str
    status: str = "applied"    # "applied" | "detected" (found, but deliberately not applied)


@dataclass
class OptimizationStats:
    constants_folded: int = 0
    algebraic_simplifications: int = 0
    subexpressions_reused: int = 0
    dead_stores_removed: int = 0
    unreachable_instructions_removed: int = 0
    copies_propagated: int = 0
    variables_eliminated: int = 0
    pointer_simplifications: int = 0
    passes_run: int = 0
    converged: bool = True
    events: List[OptEvent] = field(default_factory=list)
    skipped: List[OptEvent] = field(default_factory=list)    # detected but not applied

    def total(self) -> int:
        return (self.constants_folded + self.algebraic_simplifications
                + self.subexpressions_reused
                + self.dead_stores_removed + self.unreachable_instructions_removed
                + self.copies_propagated + self.variables_eliminated
                + self.pointer_simplifications)


# -- shared helpers --------------------------------------------------------
def _is_const(s) -> bool:
    return isinstance(s, str) and bool(NUMBER_RE.match(s))


def _const_val(s: str):
    if "." in s or "e" in s.lower():
        return float(s)
    return int(s)


def _format_const(v) -> str:
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return repr(v)
        return repr(v)
    return str(v)


def _split_blocks(instrs: List[Instr]) -> List[List[Instr]]:
    if not instrs:
        return []
    leaders = {0}
    for i, instr in enumerate(instrs):
        if instr.op == "LABEL":
            leaders.add(i)
        if instr.op in ("GOTO", "IF_FALSE", "RETURN") and i + 1 < len(instrs):
            leaders.add(i + 1)
    bounds = sorted(leaders)
    blocks = []
    for idx, start in enumerate(bounds):
        end = bounds[idx + 1] if idx + 1 < len(bounds) else len(instrs)
        blocks.append(instrs[start:end])
    return blocks


def _extract_uses(operand) -> Set[str]:
    """Returns the set of variable/temp names an operand reads from.

    Simple cases (a bare name, a constant, a string/char literal) are
    exact. Anything else -- `a[i]`, `a[i][j]`, `*p`, `s.f`, `p->f`,
    `sizeof(x)`, `(int)x`, or any combination of these -- is handled
    by one general fallback: every identifier-looking token inside the
    text is treated as a use. This doesn't need a parser for each new
    composite form (indexing, dereference, member access, casts,
    sizeof, and whatever nests inside them); it's safe because
    over-including a "use" can only block an optimisation, never cause
    a live value to be treated as dead."""
    uses: Set[str] = set()
    if operand is None:
        return uses
    if isinstance(operand, list):
        for item in operand:
            uses |= _extract_uses(item)
        return uses
    s = str(operand)
    if _is_const(s) or s.startswith('"') or s.startswith("'"):
        return uses
    if IDENT_TOKEN_RE.fullmatch(s):
        uses.add(s)
        return uses
    for tok in IDENT_TOKEN_RE.findall(s):
        if tok not in TYPE_KEYWORD_BASES:
            uses.add(tok)
    return uses


def _address_taken_vars(instrs: List[Instr]) -> Set[str]:
    """Variables whose address is taken somewhere (`&x`) -- a call can
    mutate these through a pointer even though it never names them
    directly, so CSE/copy-propagation must treat them like globals
    when invalidating cached values across a call."""
    taken: Set[str] = set()
    for ins in instrs:
        if ins.op == "UN" and ins.extra == "&":
            taken.add(_base_name(str(ins.arg1)))
    return taken


def _def_use(instr: Instr) -> Tuple[Optional[str], Set[str]]:
    dest, uses = _def_use_raw(instr)
    if dest and (_split_indexed(dest) is not None or _is_unsafe_write(dest)):
        # an element store, a write through a pointer, or a struct/union
        # member write: reads whatever the destination itself reads
        # (array/pointer/base object, plus any index expressions),
        # kills nothing -- the compiler can't know what else that write
        # may have aliased, so nothing downstream can be assumed dead.
        uses = uses | _extract_uses(dest)
        dest = None
    return dest, uses


def _def_use_raw(instr: Instr) -> Tuple[Optional[str], Set[str]]:
    if instr.op == "ASSIGN":
        return instr.dest, _extract_uses(instr.arg1)
    if instr.op == "BIN":
        return instr.dest, _extract_uses(instr.arg1) | _extract_uses(instr.arg2)
    if instr.op == "UN":
        return instr.dest, _extract_uses(instr.arg1)
    if instr.op == "CALL":
        return instr.dest, _extract_uses(instr.arg1)
    if instr.op == "CAST":
        return instr.dest, _extract_uses(instr.arg1)
    if instr.op == "RETURN":
        return None, _extract_uses(instr.arg1)
    if instr.op == "IF_FALSE":
        return None, _extract_uses(instr.arg1)
    if instr.op == "DECL" and instr.arg2 is not None:
        # an array initialiser `{a, t3, 5}` reads its element operands
        return None, _extract_uses(instr.arg2)
    return None, set()


def _map_init(init, f):
    """Applies f to every element operand of a (nested) DECL initialiser."""
    if isinstance(init, list):
        return [_map_init(x, f) for x in init]
    return f(init)


# -- type model ------------------------------------------------------------
#
# The IR keeps operands as plain strings, so on its own it cannot tell an
# `int` from a `double`. C's arithmetic depends on that distinction
# (`3 / 2` is 1, `3 / 2.0` is 1.5; assigning 2.7 to an int stores 2), so
# every pass that rewrites arithmetic consults this model instead of
# guessing. When a type cannot be determined the answer is None and the
# caller must treat the rewrite as unsafe.
INT_MIN, INT_MAX = -(2 ** 31), 2 ** 31 - 1
NUMERIC_TYPES = {"int", "char", "float", "double"}
INTEGRAL_TYPES = {"int", "char"}
COMPARISON_OPS = {"<", ">", "<=", ">=", "==", "!=", "&&", "||"}
LIBRARY_RETURNS = {"printf": "int", "scanf": "int", "puts": "int", "putchar": "int",
                   "getchar": "int", "sprintf": "int", "fprintf": "int",
                   "malloc": "void*", "calloc": "void*", "realloc": "void*"}
_CONFLICT = "?"


def _is_ptr(t) -> bool:
    return isinstance(t, str) and t.endswith("*")


def _promote(t):
    return "int" if t == "char" else t


def _const_type(text: str) -> str:
    return "double" if ("." in text or "e" in text.lower()) else "int"


def _binary_result_type(op: str, ta, tb):
    """Type of `a <op> b` under C's usual arithmetic conversions, or None
    if either operand type is unknown / the form is outside the model."""
    if op in COMPARISON_OPS:
        return "int"
    if ta is None or tb is None:
        return None
    if op in ("+", "-") and (_is_ptr(ta) or _is_ptr(tb)):
        if _is_ptr(ta) and tb in INTEGRAL_TYPES:
            return ta
        if op == "+" and _is_ptr(tb) and ta in INTEGRAL_TYPES:
            return tb
        return None
    if ta in NUMERIC_TYPES and tb in NUMERIC_TYPES:
        if op == "%" and not (ta in INTEGRAL_TYPES and tb in INTEGRAL_TYPES):
            return None
        if "double" in (ta, tb):
            return "double"
        if "float" in (ta, tb):
            return "float"
        return "int"
    return None


class OptContext:
    """Per-function facts the passes need: declared types, which names are
    arrays, the types of temporaries, and which variables can be changed
    by something other than a visible assignment (globals and anything
    whose address is taken)."""

    def __init__(self, globals_=(), global_types=None, global_arrays=None,
                 param_types=None, func_returns=None):
        self.globals = set(globals_)
        self.global_types = dict(global_types or {})
        self.global_arrays = dict(global_arrays or {})
        self.param_types = dict(param_types or {})
        self.func_returns = dict(LIBRARY_RETURNS)
        self.func_returns.update(func_returns or {})
        self.vars: Dict[str, str] = {}
        self.arrays: Dict[str, Tuple[str, int]] = {}
        self.array_dims: Dict[str, List[int]] = {}
        self.nonnull: Set[str] = set()      # pointers proven to point at a live object
        self.temps: Dict[str, str] = {}
        self.escaped: Set[str] = set(self.globals)

    # -- building ------------------------------------------------------
    def refresh(self, instrs: List[Instr]):
        self.vars = dict(self.global_types)
        self.vars.update(self.param_types)
        self.arrays = dict(self.global_arrays)
        self.array_dims = {}
        for ins in instrs:
            if ins.op == "DECL":
                if ins.arg1 is not None:
                    dims = ins.arg1 if isinstance(ins.arg1, list) else [ins.arg1]
                    self.arrays[ins.dest] = (ins.extra, len(dims))
                    self.array_dims[ins.dest] = [d for d in dims]
                    self.vars.pop(ins.dest, None)
                else:
                    self.vars[ins.dest] = ins.extra
        self.escaped = set(self.globals) | _address_taken_vars(instrs)
        self.temps = {}
        for ins in instrs:
            if (ins.op in ("ASSIGN", "BIN", "UN", "CALL", "CAST")
                    and isinstance(ins.dest, str) and _is_temp(ins.dest)):
                t = self._def_type(ins)
                prev = self.temps.get(ins.dest)
                if prev is None:
                    self.temps[ins.dest] = t if t is not None else _CONFLICT
                elif prev != t:
                    self.temps[ins.dest] = _CONFLICT
        self._compute_nonnull(instrs)

    def _compute_nonnull(self, instrs: List[Instr]):
        """Local pointer variables that always hold the address of a real
        object: every assignment to them is `p = &x` (x a plain variable, or
        an array element with a constant in-range index) or `p = array`, and
        the pointer's own address is never taken. Dereferencing such a
        pointer cannot fault, which is what lets `*p = *p` be removed."""
        self.nonnull = set()
        defs: Dict[str, List[Instr]] = {}
        for ins in instrs:
            if ins.op in ("ASSIGN", "BIN", "UN", "CAST", "CALL") and isinstance(ins.dest, str):
                defs.setdefault(ins.dest, []).append(ins)

        def valid_address_of(t: str) -> bool:
            ds = defs.get(t, [])
            if len(ds) != 1 or ds[0].op != "UN" or ds[0].extra != "&":
                return False
            target = str(ds[0].arg1)
            if IDENT_TOKEN_RE.fullmatch(target):
                return target not in self.arrays or True      # &x, or &array (address of whole array)
            return self.access_proven_valid(target, assume_nonnull=False)

        for name, t in self.vars.items():
            if (not _is_ptr(t) or name in self.param_types or name in self.globals
                    or name in self.escaped):
                continue
            ds = defs.get(name, [])
            if not ds:
                continue
            ok = True
            for d in ds:
                src = d.arg1
                if d.op == "ASSIGN" and isinstance(src, str) and _is_temp(src) \
                        and valid_address_of(src):
                    continue
                if d.op == "ASSIGN" and isinstance(src, str) and src in self.arrays:
                    continue
                ok = False
                break
            if ok:
                self.nonnull.add(name)

    def access_proven_valid(self, text: str, assume_nonnull: bool = True) -> bool:
        """True if reading or writing the memory location `text` (`*p`,
        `a[2]`, `s.f`, `p->f`, `p[0]`, ...) is guaranteed not to fault:
        it names a live object that exists for the whole function."""
        s = str(text)
        if s.startswith("*"):
            return assume_nonnull and s[1:] in self.nonnull
        m = re.match(r"^[A-Za-z_]\w*", s)
        if not m:
            return False
        name, i, n = m.group(0), m.end(), len(s)
        if name == "NULL" or _is_temp(name):
            return False
        base_is_array = name in self.arrays
        dims = list(self.array_dims.get(name, []))
        cur_ptr = name in self.nonnull if assume_nonnull else False   # a pointer to a valid object
        cur_struct = (self.vars.get(name, "").startswith(("struct ", "union "))
                      and not _is_ptr(self.vars.get(name, "")))
        if i == n:
            return True
        while i < n:
            if s[i] == "[":
                j = s.index("]", i)                       # indices here are plain constants/names
                idx = s[i + 1:j]
                i = j + 1
                if "[" in idx or not _is_const(idx) or _const_type(idx) != "int":
                    return False
                k = _const_val(idx)
                if base_is_array and dims:
                    if not (0 <= k < dims.pop(0)):
                        return False
                    if not dims:
                        base_is_array = False
                        cur_struct = self.arrays[name][0].startswith(("struct ", "union "))
                elif cur_ptr:
                    if k != 0:
                        return False
                    cur_ptr = False
                else:
                    return False
            elif s[i] == ".":
                if not (cur_struct and not base_is_array):
                    return False
                mm = re.match(r"\.([A-Za-z_]\w*)", s[i:])
                if not mm:
                    return False
                i += mm.end()
                cur_struct = False        # a member's own type is not tracked: stop at one level
                if i != n:
                    return False
            elif s.startswith("->", i):
                if not (cur_ptr and i == m.end()):
                    return False
                mm = re.match(r"->([A-Za-z_]\w*)", s[i:])
                if not mm:
                    return False
                i += mm.end()
                if i != n:
                    return False
            else:
                return False
        return not base_is_array

    def _def_type(self, ins: Instr):
        if ins.op == "ASSIGN":
            return self.type_of(ins.arg1)
        if ins.op == "BIN":
            return _binary_result_type(ins.extra, self.type_of(ins.arg1), self.type_of(ins.arg2))
        if ins.op == "CAST":
            return ins.extra
        if ins.op == "CALL":
            return self.func_returns.get(ins.extra)
        if ins.op == "UN":
            t = self.type_of(ins.arg1)
            if ins.extra in ("-", "+"):
                return _promote(t) if t in NUMERIC_TYPES else None
            if ins.extra in ("!", "~"):
                return "int"
            if ins.extra == "&":
                return t + "*" if t else None
            if ins.extra == "*":
                return t[:-1] if _is_ptr(t) else None
        return None

    # -- queries -------------------------------------------------------
    def type_of(self, operand):
        """C type of an IR operand, or None if unknown."""
        if operand is None:
            return None
        s = str(operand)
        if s in self.vars:
            return self.vars[s]
        if s in self.arrays:
            return self.arrays[s][0] + "*"          # array name decays
        if _is_const(s):
            return _const_type(s)
        if s.startswith("'"):
            return "int"                             # a char constant has type int
        if s.startswith('"'):
            return "char*"
        if s == "NULL":
            return "void*"
        if _is_temp(s):
            t = self.temps.get(s)
            return None if t in (None, _CONFLICT) else t
        if s.startswith("*"):
            t = self.type_of(s[1:])
            return t[:-1] if _is_ptr(t) else None
        split = _split_indexed(s)
        if split:
            base, idx = split
            if base in self.arrays:
                elem, ndims = self.arrays[base]
                return elem if len(idx) == ndims else None
            t = self.vars.get(base)
            for _ in idx:
                if not _is_ptr(t):
                    return None
                t = t[:-1]
            return t
        return None

    def is_array(self, name) -> bool:
        return name in self.arrays

    def writes_unknown(self, dest) -> bool:
        """True if a store to `dest` may change memory this module has no
        precise name for: a write through a pointer, a struct/union member,
        or an index into something that is not a declared array (a pointer
        or array parameter may alias any other object)."""
        if not isinstance(dest, str):
            return False
        if _is_unsafe_write(dest):
            return True
        split = _split_indexed(dest)
        return split is not None and split[0] not in self.arrays

    def convert_const(self, dest, text):
        """The constant `text` as it will actually be held by `dest` after
        C's implicit conversion, or None when that cannot be represented
        exactly enough to substitute (float/char/pointer/unknown types).
        Only an `int` or `double` destination keeps a constant whose own
        type matches exactly, which is what makes substituting the
        constant for later reads type-preserving."""
        dt = self.type_of(dest)
        if dt is None or not _is_const(text):
            return None
        ct = _const_type(text)
        v = _const_val(text)
        if dt == "int":
            if ct == "int":
                return text if INT_MIN <= v <= INT_MAX else None
            if math.isfinite(v) and INT_MIN <= int(v) <= INT_MAX:
                return str(int(v))                  # C truncates toward zero
            return None
        if dt == "double":
            return _format_const(float(v)) if math.isfinite(float(v)) else None
        if dt == "char" and ct == "int" and 0 <= v <= 127:
            return text                             # same value whether char is signed or not
        return None


def _zero_minus_hazard(op, a, b) -> bool:
    """`0.0 - x` with a literal floating-point zero on the left.

    It is exactly equal to `0.0 - x` at run time, but GCC rewrites that
    text to `-x` even when x converts from an int, and then `0.0 - 0.0`
    prints as `-0.0` instead of `0.0`. The optimiser therefore never
    *creates* that shape by substituting a known zero into the left side
    of a subtraction."""
    return (op == "-" and _is_const(a) and _const_type(a) == "double"
            and _const_val(a) == 0 and not _is_const(b))


# -- pass 1: constant folding + constant propagation ------------------------
class _NoFold(Exception):
    """A constant expression that must NOT be evaluated at compile time
    (the carried text says why)."""


def _c_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _fold_binop(op: str, a, b):
    """Evaluates `a op b` the way C does for the operand types (Python int
    -> C int, Python float -> C double). Returns None for operators this
    module does not evaluate; raises _NoFold when evaluating would be
    wrong or undefined (overflow, division by zero, non-finite result)."""
    both_int = isinstance(a, int) and isinstance(b, int)
    if both_int and not (INT_MIN <= a <= INT_MAX and INT_MIN <= b <= INT_MAX):
        raise _NoFold("an operand is outside the range of int")
    if op in ("+", "-", "*"):
        r = a + b if op == "+" else a - b if op == "-" else a * b
    elif op == "/":
        if b == 0:
            raise _NoFold("division by zero is undefined behaviour")
        r = _c_div(a, b) if both_int else a / b
    elif op == "%":
        if not both_int:
            return None
        if b == 0:
            raise _NoFold("remainder by zero is undefined behaviour")
        r = a - b * _c_div(a, b)
    elif op in ("<", ">", "<=", ">=", "==", "!="):
        r = int({"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b,
                 "==": a == b, "!=": a != b}[op])
    elif op == "&&":
        r = int(bool(a) and bool(b))
    elif op == "||":
        r = int(bool(a) or bool(b))
    else:
        return None
    if isinstance(r, float):
        if not math.isfinite(r):
            raise _NoFold("the result is not a finite number")
    elif not (INT_MIN <= r <= INT_MAX):
        raise _NoFold("the result overflows int")
    return r


def _fold_unop(op: str, a):
    if op == "-":
        r = -a
        if isinstance(r, int) and not (INT_MIN <= r <= INT_MAX):
            raise _NoFold("the result overflows int")
        return r
    if op == "!":
        return 1 if a == 0 else 0
    return None


def _show_operand(x) -> str:
    return _display(x)


def _fold_block(block: List[Instr], state: Dict[str, str], ctx: "OptContext",
                events: Optional[List[OptEvent]], skipped: Optional[List[OptEvent]]
                ) -> Tuple[List[Instr], Dict[str, str], int]:
    """Folds and propagates through one basic block, starting from `state`
    (variable -> the constant it is known to hold on entry). Returns the
    rewritten block, the state at its end, and the number of folds."""
    state = dict(state)
    out: List[Instr] = []
    folded = 0

    def note(ins, kind, msg):
        if events is not None:
            events.append(OptEvent(ins.line, kind, msg))

    def refuse(ins, expr, why):
        if skipped is not None:
            skipped.append(OptEvent(ins.line, "fold",
                f"'{expr}' is a constant expression but was not evaluated: {why}",
                status="detected"))

    def after_write(dest):
        if not isinstance(dest, str):
            return
        if ctx.writes_unknown(dest):
            # a store through an unknown pointer can only reach variables
            # whose address escaped (or globals) -- other locals are safe
            for name in list(state):
                if name in ctx.escaped:
                    del state[name]
        else:
            state.pop(_base_name(dest), None)

    def record(dest, text):
        """Remember dest == text, converted to dest's own type."""
        if not isinstance(dest, str) or _split_indexed(dest) is not None \
                or _is_unsafe_write(dest):
            return None
        conv = ctx.convert_const(dest, text)
        if conv is not None:
            state[dest] = conv
        return conv

    for instr in block:
        op = instr.op

        if op == "LABEL" or op == "GOTO":
            out.append(instr)
            continue

        if op == "DECL":
            state.pop(instr.dest, None)
            if isinstance(instr.arg2, list):
                instr = Instr("DECL", dest=instr.dest, arg1=instr.arg1, arg2=_map_init(
                    instr.arg2, lambda x: state.get(x, x)), extra=instr.extra, line=instr.line)
            out.append(instr)
            continue

        if op == "ASSIGN":
            src = state.get(instr.arg1, instr.arg1)
            after_write(instr.dest)
            conv = record(instr.dest, src) if _is_const(src) else None
            out.append(Instr("ASSIGN", dest=instr.dest, arg1=conv if conv is not None else src,
                             line=instr.line))
            continue

        if op == "BIN":
            a = state.get(instr.arg1, instr.arg1)
            b = state.get(instr.arg2, instr.arg2)
            if _zero_minus_hazard(instr.extra, a, b) and not _is_const(instr.arg1):
                a = instr.arg1
            if _is_const(a) and _is_const(b):
                expr = f"{a} {instr.extra} {b}"
                try:
                    result = _fold_binop(instr.extra, _const_val(a), _const_val(b))
                except _NoFold as why:
                    refuse(instr, expr, str(why))
                    result = None
                if result is not None:
                    text = _format_const(result)
                    after_write(instr.dest)
                    conv = record(instr.dest, text)
                    folded += 1
                    note(instr, "fold", f"'{expr}' evaluated at compile time -> "
                                        f"{conv if conv is not None else text}")
                    out.append(Instr("ASSIGN", dest=instr.dest,
                                     arg1=conv if conv is not None else text, line=instr.line))
                    continue
            out.append(Instr("BIN", dest=instr.dest, arg1=a, arg2=b, extra=instr.extra,
                             line=instr.line))
            after_write(instr.dest)
            continue

        if op == "UN":
            if instr.extra == "&":
                # `&x` needs the variable itself, never a value known to be
                # substitutable for it -- constant-propagating into it would
                # silently take the address of a literal.
                out.append(instr)
                continue
            a = state.get(instr.arg1, instr.arg1)
            if _is_const(a):
                try:
                    result = _fold_unop(instr.extra, _const_val(a))
                except _NoFold as why:
                    refuse(instr, f"{instr.extra}{a}", str(why))
                    result = None
                if result is not None:
                    text = _format_const(result)
                    after_write(instr.dest)
                    conv = record(instr.dest, text)
                    folded += 1
                    note(instr, "fold", f"'{instr.extra}{a}' evaluated at compile time -> "
                                        f"{conv if conv is not None else text}")
                    out.append(Instr("ASSIGN", dest=instr.dest,
                                     arg1=conv if conv is not None else text, line=instr.line))
                    continue
            out.append(Instr("UN", dest=instr.dest, arg1=a, extra=instr.extra, line=instr.line))
            after_write(instr.dest)
            continue

        if op == "CAST":
            a = state.get(instr.arg1, instr.arg1)
            if _is_const(a) and instr.extra in ("int", "double"):
                v = _const_val(a)
                if instr.extra == "double":
                    text = _format_const(float(v))
                elif isinstance(v, int) or (math.isfinite(v) and INT_MIN <= int(v) <= INT_MAX):
                    text = str(int(v))
                else:
                    text = None
                if text is not None and _is_const(text):
                    after_write(instr.dest)
                    conv = record(instr.dest, text)
                    folded += 1
                    note(instr, "fold", f"'({instr.extra}){a}' evaluated at compile time -> {text}")
                    out.append(Instr("ASSIGN", dest=instr.dest, arg1=text, line=instr.line))
                    continue
            out.append(Instr("CAST", dest=instr.dest, arg1=a, extra=instr.extra, line=instr.line))
            after_write(instr.dest)
            continue

        if op == "IF_FALSE":
            a = state.get(instr.arg1, instr.arg1)
            if _is_const(a):
                folded += 1
                if _const_val(a) == 0:
                    note(instr, "fold", "condition is always false -> branch is always taken")
                    out.append(Instr("GOTO", dest=instr.dest, line=instr.line))
                else:
                    note(instr, "fold", "condition is always true -> test removed")
                continue
            out.append(Instr("IF_FALSE", dest=instr.dest, arg1=a, line=instr.line))
            continue

        if op == "RETURN":
            a = state.get(instr.arg1, instr.arg1) if instr.arg1 is not None else None
            out.append(Instr("RETURN", arg1=a, line=instr.line))
            continue

        if op == "CALL":
            args = [state.get(a, a) for a in (instr.arg1 or [])]
            out.append(Instr("CALL", dest=instr.dest, arg1=args, extra=instr.extra, line=instr.line))
            if instr.dest:
                state.pop(instr.dest, None)
            # a callee may rewrite any global, or anything whose address
            # was handed to it (directly, or earlier, through &x)
            for name in list(state):
                if name in ctx.escaped:
                    del state[name]
            continue

        out.append(instr)

    return out, state, folded


def _meet(a: Dict[str, str], b: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in a.items() if b.get(k) == v}


def constant_folding(instrs: List[Instr], ctx: "OptContext",
                     events: Optional[List[OptEvent]] = None,
                     skipped: Optional[List[OptEvent]] = None) -> Tuple[List[Instr], int]:
    """Constant folding and propagation over the whole function.

    Which variables hold a known constant at the start of every basic
    block is computed by a forward data-flow analysis over the CFG
    (a variable is a constant at a join only if every incoming path
    agrees), so a value assigned before an `if` or a loop is propagated
    into and past it -- not just within a single straight-line block.
    Branches whose condition becomes constant are resolved here and the
    code they bypass is removed by the dead-code pass."""
    blocks = _split_blocks(instrs)
    if not blocks:
        return instrs, 0
    succ = _build_cfg(blocks)
    n = len(blocks)
    entry: List[Optional[Dict[str, str]]] = [None] * n
    entry[0] = {}
    work = [0]
    budget = 200 * n + 1000          # the lattice only shrinks, so this is a safety net
    while work and budget > 0:
        budget -= 1
        b = work.pop(0)
        _, end_state, _ = _fold_block(blocks[b], entry[b], ctx, None, None)
        for s in succ[b]:
            new = dict(end_state) if entry[s] is None else _meet(entry[s], end_state)
            if entry[s] is None or new != entry[s]:
                entry[s] = new
                if s not in work:
                    work.append(s)
    if work:                          # did not settle: fall back to no propagation across blocks
        entry = [{} for _ in range(n)]

    out: List[Instr] = []
    folded = 0
    for i, blk in enumerate(blocks):
        o, _, f = _fold_block(blk, entry[i] if entry[i] is not None else {}, ctx, events, skipped)
        out.extend(o)
        folded += f
    return out, folded


# -- pass 1b: algebraic simplification --------------------------------------
def algebraic_simplification(instrs: List[Instr], ctx: "OptContext",
                             events: Optional[List[OptEvent]] = None,
                             skipped: Optional[List[OptEvent]] = None
                             ) -> Tuple[List[Instr], int]:
    """Rewrites identities that constant folding can't, because only one
    side is a known constant. Each rewrite is applied only when the
    operand types are known and the identity holds *for that type*:

      x + 0, 0 + x, x * 0, 0 * x, x - x   -- integers only. For floating
        point, `x + 0` turns -0.0 into +0.0 and `x * 0` is NaN for an
        infinite or NaN x and -0.0 for a negative x, so those are
        detected and reported but left alone.
      x - 0, x * 1, 1 * x, x / 1         -- exact for every numeric type.

    The replacement must also have the same type as the original
    expression (after integer promotion): `i * 1.0` is a double, so
    replacing it with the int `i` would change later arithmetic."""
    out: List[Instr] = []
    count = 0
    events = events if events is not None else []

    def is_zero(x):
        return _is_const(x) and _const_val(x) == 0

    def is_one(x):
        return _is_const(x) and _const_val(x) == 1

    for instr in instrs:
        if instr.op != "BIN":
            out.append(instr)
            continue

        a, b, op = instr.arg1, instr.arg2, instr.extra
        ta, tb = ctx.type_of(a), ctx.type_of(b)
        result_t = _binary_result_type(op, ta, tb)
        replacement = None
        needs_integral = None      # operand that must be integral for the identity

        if op == "+" and is_zero(b):
            replacement, needs_integral = a, a
        elif op == "+" and is_zero(a):
            replacement, needs_integral = b, b
        elif op == "-" and is_zero(b):
            replacement = a
        elif op == "*" and (is_zero(a) or is_zero(b)):
            other = b if is_zero(a) else a
            replacement, needs_integral = "0", other
        elif op == "*" and is_one(b):
            replacement = a
        elif op == "*" and is_one(a):
            replacement = b
        elif op == "/" and is_one(b):
            replacement = a
        elif op == "-" and a == b and (_is_plain_name(a) or _is_temp(a)):
            replacement, needs_integral = "0", a

        if replacement is None:
            out.append(instr)
            continue

        label = f"{_show_operand(a)} {op} {_show_operand(b)}"
        applicable = result_t is not None
        reason = "operand types are not known"
        if applicable and needs_integral is not None:
            t_need = ctx.type_of(needs_integral)
            if t_need not in INTEGRAL_TYPES:
                applicable = False
                reason = ("the identity is not exact for floating-point values "
                          "(-0.0, NaN, infinity)" if t_need in ("float", "double")
                          else "operand type is not an integer")
        if applicable:
            rt = ctx.type_of(replacement)
            if replacement == "0":
                applicable = (result_t == "int")
            else:
                applicable = rt is not None and _promote(rt) == result_t
            if not applicable:
                reason = "the replacement would change the type of the expression"

        if not applicable:
            if skipped is not None:
                skipped.append(OptEvent(instr.line, "algebraic",
                    f"'{label}' matches an algebraic identity but was left alone: {reason}",
                    status="detected"))
            out.append(instr)
            continue

        count += 1
        events.append(OptEvent(instr.line, "algebraic",
            f"'{label}' simplified to '{_show_operand(replacement)}'"))
        out.append(Instr("ASSIGN", dest=instr.dest, arg1=replacement, line=instr.line))

    return out, count


# -- pass 2: common subexpression elimination (per basic block) -----------
def _memory_kind(operand, ctx: "OptContext") -> Optional[str]:
    """None for a plain name/constant; "array" for an element of a declared
    array; "other" for anything read through a pointer, a struct member,
    or an index into a non-array (which may alias other objects)."""
    if not isinstance(operand, str) or _is_const(operand) or operand[:1] in ("'", '"'):
        return None
    if operand.startswith("*") or "->" in operand or "." in operand:
        return "other"
    split = _split_indexed(operand)
    if split:
        return "array" if split[0] in ctx.arrays else "other"
    return None


def common_subexpression_elimination(instrs: List[Instr], ctx: "OptContext",
                                     events: Optional[List[OptEvent]] = None
                                     ) -> Tuple[List[Instr], int]:
    blocks = _split_blocks(instrs)
    result: List[Instr] = []
    reused = 0
    events = events if events is not None else []

    for block in blocks:
        # key -> (holder, line where first computed)
        cache: Dict[Tuple[str, str, str], Tuple[str, int]] = {}

        def drop(pred):
            for k in [k for k, v in cache.items() if pred(k, v)]:
                cache.pop(k, None)

        for instr in block:
            new_instr = instr
            hit = None
            if instr.op == "BIN":
                key = (instr.extra, str(instr.arg1), str(instr.arg2))
                alt_key = (instr.extra, str(instr.arg2), str(instr.arg1)) \
                    if instr.extra in COMMUTATIVE_OPS else None
                hit = cache.get(key) or (cache.get(alt_key) if alt_key else None)
                if hit is not None:
                    holder, first_line = hit
                    reused += 1
                    events.append(OptEvent(instr.line, "cse",
                        f"'{_show_operand(instr.arg1)} {instr.extra} {_show_operand(instr.arg2)}' "
                        f"already computed at line {first_line}; value reused"))
                    new_instr = Instr("ASSIGN", dest=instr.dest, arg1=holder, line=instr.line)
            result.append(new_instr)

            # -- invalidate cached expressions this instruction could change --
            raw_dest = new_instr.dest if new_instr.op in ("ASSIGN", "BIN", "UN", "CALL", "CAST") else None
            if raw_dest:
                if ctx.writes_unknown(raw_dest):
                    cache.clear()          # may alias anything whose address escaped
                else:
                    base = _base_name(raw_dest)
                    split = _split_indexed(raw_dest)
                    drop(lambda k, v: base in _extract_uses(k[1]) or base in _extract_uses(k[2])
                         or _base_name(v[0]) == base)
                    if split is not None:
                        # element store into a declared array: pointer-based
                        # reads might be looking at that same array
                        drop(lambda k, v: "other" in (_memory_kind(k[1], ctx), _memory_kind(k[2], ctx)))
                    elif base in ctx.escaped:
                        # a plain write to a variable whose address escaped
                        # changes what `*p` / `p[i]` / `s->f` read
                        drop(lambda k, v: "other" in (_memory_kind(k[1], ctx), _memory_kind(k[2], ctx)))
            if new_instr.op == "CALL":
                # a callee may change globals, arrays, anything whose address
                # escaped to it, and anything reached through a pointer
                drop(lambda k, v: any(_memory_kind(o, ctx) is not None
                                      or _extract_uses(o) & ctx.escaped for o in (k[1], k[2])))

            # -- remember this computation --------------------------------
            if instr.op == "BIN" and hit is None:
                dest = instr.dest
                uses = _extract_uses(instr.arg1) | _extract_uses(instr.arg2)
                holder_ok = (isinstance(dest, str) and _split_indexed(dest) is None
                             and not _is_unsafe_write(dest)
                             and _base_name(dest) not in uses            # `c = c + d` is NOT "c + d"
                             and (_is_temp(dest)
                                  or ctx.type_of(dest) == _binary_result_type(
                                      instr.extra, ctx.type_of(instr.arg1), ctx.type_of(instr.arg2))))
                if holder_ok:
                    cache[key] = (dest, instr.line)

    return result, reused


# -- pass 2b: copy propagation (per basic block) ------------------------------
def _is_plain_name(x) -> bool:
    return isinstance(x, str) and IDENT_TOKEN_RE.fullmatch(x) is not None


def copy_propagation(instrs: List[Instr], ctx: "OptContext",
                     events: Optional[List[OptEvent]] = None) -> Tuple[List[Instr], int]:
    """After `y = t1`, later reads of t1 can read y directly (and
    `t2 = y; z = t2` becomes `z = y`). A copy between two *user*
    variables (or a temporary and a variable of a different type) is
    only propagated when both have exactly the same declared type:
    `double d = i; ... d / 4` must not become `i / 4`."""
    out: List[Instr] = []
    alias: Dict[str, str] = {}
    count = 0
    events = events if events is not None else []
    cur_line = 0

    def subst(x):
        nonlocal count
        if isinstance(x, list):
            return [subst(i) for i in x]
        if isinstance(x, str) and x in alias:
            count += 1
            if not _is_temp(x):
                events.append(OptEvent(cur_line, "copy",
                    f"'{_display(x)}' is a copy of '{_display(alias[x])}'; "
                    f"the original is read directly"))
            return alias[x]
        if isinstance(x, str) and x.startswith("*"):
            inner = subst(x[1:])
            return "*" + inner if inner != x[1:] else x
        if isinstance(x, str):
            split = _split_indexed(x)
            if split:
                name, indices = split
                new_indices = [subst(idx) for idx in indices]
                if new_indices != indices:
                    count += 1
                    return name + "".join(f"[{i}]" for i in new_indices)
        return x

    def kill(name: str):
        base = _base_name(name)
        for k in [k for k, v in alias.items() if k == base or v == base]:
            alias.pop(k, None)

    def drop_escaped():
        for k in [k for k, v in alias.items() if k in ctx.escaped or v in ctx.escaped]:
            alias.pop(k, None)

    for instr in instrs:
        cur_line = instr.line
        if instr.op == "LABEL":
            alias.clear()
            out.append(instr)
            continue
        if instr.op == "UN" and instr.extra == "&":
            # `&x` needs x itself -- substituting a value-equal alias
            # would take the address of the wrong variable.
            out.append(instr)
            continue
        new = Instr(instr.op, dest=instr.dest, arg1=subst(instr.arg1),
                    arg2=subst(instr.arg2), extra=instr.extra, line=instr.line)
        if instr.op in ("IF_FALSE", "GOTO", "RETURN"):
            out.append(new)
            continue
        if instr.op == "DECL":
            kill(instr.dest)
            out.append(new)
            continue
        if ctx.writes_unknown(new.dest):
            drop_escaped()
        elif new.dest:
            kill(new.dest)
        if new.op == "CALL":
            drop_escaped()
        if (new.op == "ASSIGN" and _is_plain_name(new.arg1) and new.arg1 != "NULL"
                and new.arg1 != new.dest and _is_plain_name(new.dest)
                and new.dest not in ctx.escaped and new.arg1 not in ctx.escaped
                and new.dest not in ctx.arrays and new.arg1 not in ctx.arrays):
            td, ts = ctx.type_of(new.dest), ctx.type_of(new.arg1)
            if _is_temp(new.dest):
                alias[new.dest] = new.arg1            # t = v   : read v instead of t
            elif ts is not None and td == ts:
                if _is_temp(new.arg1):
                    alias[new.arg1] = new.dest        # v = t   : read v instead of t
                else:
                    alias[new.dest] = new.arg1        # v = w   : read w instead of v
        out.append(new)

    return out, count


# -- pass 2d: single-use variable elimination ---------------------------------
def _token_counts(operand, counts: Dict[str, int]):
    if operand is None:
        return
    if isinstance(operand, list):
        for item in operand:
            _token_counts(item, counts)
        return
    text = str(operand)
    if text[:1] in ("'", '"'):
        return
    for tok in IDENT_TOKEN_RE.findall(text):
        if tok not in TYPE_KEYWORD_BASES:
            counts[tok] = counts.get(tok, 0) + 1


def _read_counts(instr: Instr) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    _token_counts(instr.arg1, counts)
    _token_counts(instr.arg2, counts)
    if (instr.op in ("ASSIGN", "BIN", "UN", "CALL", "CAST") and isinstance(instr.dest, str)
            and not IDENT_TOKEN_RE.fullmatch(instr.dest)):
        _token_counts(instr.dest, counts)        # `a[i] = ...` / `*p = ...` read a, i / p
    return counts


def _replace_whole_operand(instr: Instr, name: str, replacement: str) -> Instr:
    def sub(x):
        if isinstance(x, list):
            return [sub(i) for i in x]
        return replacement if x == name else x
    return Instr(instr.op, dest=instr.dest, arg1=sub(instr.arg1), arg2=sub(instr.arg2),
                 extra=instr.extra, line=instr.line)


def eliminate_single_use_variables(instrs: List[Instr], ctx: "OptContext",
                                   events: Optional[List[OptEvent]] = None
                                   ) -> Tuple[List[Instr], int]:
    """`int r = a + b; ... return r;` -> `return a + b;`

    A local variable that is assigned exactly once and read exactly once
    only carries a value from one statement to the next. The read is
    rewritten to use the assigned value directly and the variable (and its
    declaration) disappears. Applied only when all of these hold:
      * the variable is a plain non-escaped scalar with one definition and
        one read in the whole function;
      * the read is in the same basic block, after the definition, as a
        whole operand (not inside `a[v]` / `*v`);
      * nothing between them could change the value being forwarded (no
        write to the source, no call or memory write if the source is
        memory or a global) and no control flow separates them;
      * the variable and the value have the same known type, so dropping
        the intermediate store drops no conversion."""
    events = events if events is not None else []
    total_reads: Dict[str, int] = {}
    defs: Dict[str, int] = {}
    for ins in instrs:
        for tok, n in _read_counts(ins).items():
            total_reads[tok] = total_reads.get(tok, 0) + n
        if ins.op in ("ASSIGN", "BIN", "UN", "CAST", "CALL") and isinstance(ins.dest, str) \
                and IDENT_TOKEN_RE.fullmatch(ins.dest):
            defs[ins.dest] = defs.get(ins.dest, 0) + 1

    out = list(instrs)
    removed_idx: Set[int] = set()
    count = 0
    for i, d in enumerate(instrs):
        if i in removed_idx or d.op != "ASSIGN" or not isinstance(d.arg1, str):
            continue
        v, src = d.dest, d.arg1
        if (not isinstance(v, str) or not IDENT_TOKEN_RE.fullmatch(v) or _is_temp(v)
                or v not in ctx.vars or v in ctx.escaped or v in ctx.arrays
                or total_reads.get(v, 0) != 1 or defs.get(v, 0) != 1 or src == v):
            continue
        tv, ts = ctx.type_of(v), ctx.type_of(src)
        if tv is None or tv != ts:
            continue
        src_uses = _extract_uses(src)
        src_is_memory = _memory_kind(src, ctx) is not None
        src_is_shared = src_is_memory or bool(src_uses & ctx.escaped)
        found = None
        for k in range(i + 1, len(instrs)):
            ins = out[k]
            if k in removed_idx:
                continue
            if ins.op in ("LABEL", "GOTO"):
                break
            reads = _read_counts(ins)
            if reads.get(v, 0):
                found = k
                break
            if ins.op in ("IF_FALSE", "RETURN"):
                break
            if ins.op == "DECL":
                continue
            if ins.op == "CALL" and src_is_shared:
                break
            w = ins.dest if ins.op in ("ASSIGN", "BIN", "UN", "CAST", "CALL") else None
            if isinstance(w, str):
                if w == v or _base_name(w) in src_uses or w in src_uses:
                    break
                if src_is_shared and (ctx.writes_unknown(w) or _split_indexed(w) is not None
                                      or w in ctx.escaped):
                    break
        if found is None:
            continue
        use = out[found]
        if use.op == "DECL" or (use.op == "UN" and use.extra == "&"):
            continue
        new_use = _replace_whole_operand(use, v, src)
        if _read_counts(new_use).get(v, 0):       # `v` also appeared inside a bigger operand
            continue
        if new_use.op == "BIN" and _zero_minus_hazard(new_use.extra, new_use.arg1, new_use.arg2):
            continue
        out[found] = new_use
        removed_idx.add(i)
        count += 1
        events.append(OptEvent(d.line, "inline",
            f"variable '{_display(v)}' was assigned once and read once; its value is "
            f"used at the point of use and the variable removed"))
    if not removed_idx:
        return instrs, 0
    return [ins for idx, ins in enumerate(out) if idx not in removed_idx], count


# -- pass 2e: pointer-arithmetic to array-index simplification -------------
def simplify_pointer_arithmetic(instrs: List[Instr], ctx: "OptContext",
                                events: Optional[List[OptEvent]] = None
                                ) -> Tuple[List[Instr], int]:
    """`*(p + i)` (read) and `*(p + i) = v` (write) are, by the C standard
    (6.5.2.1), exactly the same expression as `p[i]` -- `E1[E2]` is defined
    to mean `*((E1)+(E2))`. Rewriting the one IR idiom as the other is
    therefore not an approximation that depends on proving anything about
    the pointer's value: it changes no behaviour at all, for any pointer
    (NULL, out of range, or valid) and either operand order (`*(p + i)`
    or `*(i + p)`), because both spellings fault together or succeed
    together on exactly the same address.

    Applied only to the idiom this compiler actually emits for `*(p + i)`
    -- a compiler temporary defined by one `BIN +` and read exactly once
    in the whole function, with nothing between its definition and that
    one read that could change `p` or `i` (a write to either, or a call
    that might, through a global or an escaped variable) -- so nothing
    else in the function can depend on the intermediate pointer value
    being kept around, and the index expression still means the same
    thing at the point it is moved to."""
    events = events if events is not None else []

    reads: Dict[str, int] = {}
    defs: Dict[str, int] = {}
    for ins in instrs:
        uses = _extract_uses(ins.arg1) | _extract_uses(ins.arg2)
        if isinstance(ins.dest, str) and (ins.dest.startswith("*") or _is_unsafe_write(ins.dest)
                                          or _split_indexed(ins.dest) is not None):
            uses |= _extract_uses(ins.dest)
        for u in uses:
            reads[u] = reads.get(u, 0) + 1
        if (ins.op in ("ASSIGN", "BIN", "UN", "CALL", "CAST") and isinstance(ins.dest, str)
                and IDENT_TOKEN_RE.fullmatch(ins.dest)):
            defs[ins.dest] = defs.get(ins.dest, 0) + 1

    def index_form(a, b) -> Optional[str]:
        ta, tb = ctx.type_of(a), ctx.type_of(b)
        if _is_ptr(ta) and tb in INTEGRAL_TYPES:
            return f"{_show_operand(a)}[{_show_operand(b)}]"
        if _is_ptr(tb) and ta in INTEGRAL_TYPES:
            return f"{_show_operand(b)}[{_show_operand(a)}]"
        return None

    out = list(instrs)
    removed_idx: Set[int] = set()
    count = 0
    n = len(instrs)
    for i, cur in enumerate(instrs):
        if (i in removed_idx or cur.op != "BIN" or cur.extra != "+" or not isinstance(cur.dest, str)
                or not _is_temp(cur.dest) or defs.get(cur.dest, 0) != 1
                or reads.get(cur.dest, 0) != 1):
            continue
        idx = index_form(cur.arg1, cur.arg2)
        if idx is None:
            continue
        src_uses = _extract_uses(cur.arg1) | _extract_uses(cur.arg2)
        src_is_shared = bool(src_uses & ctx.escaped)
        found = None
        for k in range(i + 1, n):
            if k in removed_idx:
                continue
            ins = out[k]
            if ins.op in ("LABEL", "GOTO", "IF_FALSE", "RETURN"):
                break
            is_load = ins.op == "UN" and ins.extra == "*" and ins.arg1 == cur.dest
            is_store = ins.op == "ASSIGN" and ins.dest == "*" + str(cur.dest)
            if is_load or is_store:
                found = k
                break
            if ins.op == "DECL":
                continue
            if ins.op == "CALL" and src_is_shared:
                break
            w = ins.dest if ins.op in ("ASSIGN", "BIN", "UN", "CAST", "CALL") else None
            if isinstance(w, str):
                if _base_name(w) in src_uses or w in src_uses:
                    break
                if src_is_shared and (ctx.writes_unknown(w) or w in ctx.escaped):
                    break
        if found is None:
            continue
        use = out[found]
        label = f"*({_show_operand(cur.arg1)} + {_show_operand(cur.arg2)})"
        if use.op == "UN":
            out[found] = Instr("ASSIGN", dest=use.dest, arg1=idx, line=use.line)
            msg = f"'{label}' is the same memory location as '{idx}'; rewritten as array indexing"
        else:
            out[found] = Instr("ASSIGN", dest=idx, arg1=use.arg1, line=use.line)
            msg = (f"'{label} = ...' is the same memory location as '{idx} = ...'; "
                   f"rewritten as array indexing")
        removed_idx.add(i)
        count += 1
        events.append(OptEvent(use.line, "pointer", msg))
    if not removed_idx:
        return instrs, 0
    return [ins for idx, ins in enumerate(out) if idx not in removed_idx], count


# -- pass 2c: redundant self-assignment elimination ------------------------
def remove_redundant_self_assignments(instrs: List[Instr], ctx: "OptContext",
                                      events: Optional[List[OptEvent]] = None,
                                      skipped: Optional[List[OptEvent]] = None
                                      ) -> Tuple[List[Instr], int]:
    """Drops stores that write back the value already there:

      x = x            a plain variable (also what `c = c + 0` and
                       `w = v; v = w;` turn into after other passes)
      *p = *p, a[2] = a[2], s.f = s.f, p->f = p->f
                       the same location named on both sides
      x = *p; *p = x   a value loaded from a location and stored straight
                       back, with nothing in between that could change
                       either the location or the value

    A store to memory is removed only when the location is *proven* valid
    (`OptContext.access_proven_valid`): deleting `*p = *p` for a pointer
    that might be NULL would turn a crash into a success. Cases that are
    self-assignments but not proven safe are reported as detected and kept."""
    out: List[Instr] = []
    removed = 0
    events = events if events is not None else []
    loaded: Dict[str, str] = {}          # name -> memory location it was loaded from (still valid)

    def invalidate_for_write(dest):
        if not isinstance(dest, str):
            return
        if ctx.writes_unknown(dest) or _split_indexed(dest) is not None:
            loaded.clear()
            return
        base = _base_name(dest)
        loaded.pop(base, None)
        for k in [k for k, loc in loaded.items() if base in _extract_uses(loc)]:
            loaded.pop(k, None)
        if base in ctx.escaped:
            loaded.clear()               # `*p` / `p[i]` may read exactly this variable

    for instr in instrs:
        if instr.op == "LABEL":
            loaded.clear()
            out.append(instr)
            continue

        if instr.op == "ASSIGN" and isinstance(instr.dest, str) and isinstance(instr.arg1, str):
            d, sres = instr.dest, instr.arg1
            # -- plain `x = x`
            if d == sres and _is_plain_name(d):
                removed += 1
                events.append(OptEvent(instr.line, "dead-store",
                    f"redundant self-assignment '{_display(d)} = {_display(d)}' removed"))
                continue
            # -- the same memory location on both sides, or a load stored straight back
            if _memory_kind(d, ctx) is not None or d.startswith("*"):
                same_text = (d == sres)
                written_back = (loaded.get(sres) == d
                                and ctx.type_of(sres) is not None
                                and ctx.type_of(sres) == ctx.type_of(d))
                if same_text or written_back:
                    if ctx.access_proven_valid(d):
                        removed += 1
                        what = f"{_display(d)} = {_display(loaded.get(sres, sres))}"
                        events.append(OptEvent(instr.line, "dead-store",
                            f"redundant self-assignment '{what}' removed"
                            if same_text else
                            f"'{_display(d)}' is stored back unchanged ('{what}'); store removed"))
                        continue
                    if skipped is not None:
                        skipped.append(OptEvent(instr.line, "dead-store",
                            f"'{_display(d)} = {_display(loaded.get(sres, sres))}' writes a value back to where it came "
                            f"from, but the location is not proven valid (it could be NULL or out of "
                            f"range), so the store was kept", status="detected"))

        # -- keep track of "name holds the value of memory location X"
        if instr.op == "UN" and instr.extra == "*" and _is_temp(str(instr.dest)):
            invalidate_for_write(instr.dest)
            loaded[instr.dest] = "*" + str(instr.arg1)
        elif (instr.op == "ASSIGN" and isinstance(instr.arg1, str) and isinstance(instr.dest, str)
              and _memory_kind(instr.arg1, ctx) is not None and _is_plain_name(instr.dest)
              and instr.dest not in _extract_uses(instr.arg1)):
            invalidate_for_write(instr.dest)
            loaded[instr.dest] = instr.arg1
        elif (instr.op == "ASSIGN" and isinstance(instr.arg1, str) and isinstance(instr.dest, str)
              and instr.arg1 in loaded and _is_plain_name(instr.dest) and instr.dest != instr.arg1
              and ctx.type_of(instr.dest) is not None
              and ctx.type_of(instr.dest) == ctx.type_of(instr.arg1)):
            # `t = t0` where t0 holds *p: t holds it too (same type, so no conversion)
            location = loaded[instr.arg1]
            invalidate_for_write(instr.dest)
            if instr.dest not in _extract_uses(location):
                loaded[instr.dest] = location
        else:
            if instr.op in ("ASSIGN", "BIN", "UN", "CAST", "CALL") and instr.dest:
                invalidate_for_write(instr.dest)
            if instr.op == "CALL":
                loaded.clear()
        out.append(instr)
    return out, removed


# -- pass 3: dead code elimination -----------------------------------------
def _build_cfg(blocks: List[List[Instr]]):
    label_to_block = {
        blk[0].dest: i for i, blk in enumerate(blocks) if blk and blk[0].op == "LABEL"
    }
    succ: List[List[int]] = [[] for _ in blocks]
    for i, blk in enumerate(blocks):
        if not blk:
            succ[i] = [i + 1] if i + 1 < len(blocks) else []
            continue
        last = blk[-1]
        if last.op == "GOTO":
            succ[i] = [label_to_block[last.dest]] if last.dest in label_to_block else []
        elif last.op == "IF_FALSE":
            targets = []
            if i + 1 < len(blocks):
                targets.append(i + 1)
            if last.dest in label_to_block:
                targets.append(label_to_block[last.dest])
            succ[i] = targets
        elif last.op == "RETURN":
            succ[i] = []
        else:
            succ[i] = [i + 1] if i + 1 < len(blocks) else []
    return succ


def _may_fault(instr: Instr, ctx: Optional["OptContext"] = None) -> bool:
    """True if executing `instr` could trap or crash even though its result
    is unused: an integer division or remainder whose divisor is not a
    known safe constant (division by zero, INT_MIN / -1), or a load through
    a pointer (`*p` with p == NULL). Removing such an instruction because
    its value is dead would turn a program that crashes into one that does
    not, so the dead-code passes keep it. (Floating-point division never
    traps, and array/member reads are only ever dead as part of a larger
    instruction, so they are not treated this way.)"""
    if instr.op == "BIN" and instr.extra in ("/", "%"):
        if ctx is not None:
            ta, tb = ctx.type_of(instr.arg1), ctx.type_of(instr.arg2)
            if ta in ("float", "double") or tb in ("float", "double"):
                return False
        b = instr.arg2
        return not (_is_const(b) and _const_val(b) not in (0, -1))
    if instr.op == "UN" and instr.extra == "*":
        return not (ctx is not None and str(instr.arg1) in ctx.nonnull)
    return False


def _note_kept(skipped, instr: Instr, dest):
    if skipped is None:
        return
    what = (f"{_show_operand(instr.arg1)} {instr.extra} {_show_operand(instr.arg2)}"
            if instr.op == "BIN" else f"{instr.extra}{_show_operand(instr.arg1)}")
    skipped.append(OptEvent(instr.line, "dead-store",
        f"result of '{what}' is never used, but the operation can fault "
        f"(division by zero / null dereference), so it was kept", status="detected"))


def dead_code_elimination(instrs: List[Instr], globals_: Set[str] = frozenset(),
                          events: Optional[List[OptEvent]] = None,
                          skipped: Optional[List[OptEvent]] = None,
                          may_fault=None) -> Tuple[List[Instr], int, int]:
    events = events if events is not None else []
    may_fault = may_fault or (lambda ins: False)
    blocks = _split_blocks(instrs)
    if not blocks:
        return instrs, 0, 0
    succ = _build_cfg(blocks)
    n = len(blocks)

    # -- unreachable-code removal: anything not reachable from block 0 ----
    reachable = {0}
    frontier = [0]
    while frontier:
        cur = frontier.pop()
        for nxt in succ[cur]:
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    unreachable_removed = 0
    for i in range(n):
        if i in reachable:
            continue
        real = [x for x in blocks[i] if x.op not in ("LABEL", "DECL")]
        lines = [x.line for x in real if x.line]
        if real and lines:
            lo, hi = min(lines), max(lines)
            span = f"line {lo}" if lo == hi else f"lines {lo}-{hi}"
            events.append(OptEvent(lo, "unreachable", f"unreachable code at {span} removed"))
        unreachable_removed += len(real)
    # declarations in unreachable blocks must survive: the variable is still in scope
    keep_decls = [[x for x in blocks[i] if x.op == "DECL"] if i not in reachable else None
                  for i in range(n)]

    # -- iterative backward liveness over the (reachable) CFG -------------
    live_in: List[Set[str]] = [set() for _ in range(n)]
    live_out: List[Set[str]] = [set() for _ in range(n)]

    def block_live_in(i: int, out_set: Set[str]) -> Set[str]:
        live = set(out_set)
        for instr in reversed(blocks[i]):
            dest, uses = _def_use(instr)
            if dest:
                live.discard(dest)
            live |= uses
        return live

    changed = True
    guard = 0
    while changed and guard < 50:
        changed = False
        guard += 1
        for i in reversed(range(n)):
            if i not in reachable:
                continue
            new_out = set()
            for s in succ[i]:
                new_out |= live_in[s]
            if new_out != live_out[i]:
                live_out[i] = new_out
                changed = True
            new_in = block_live_in(i, live_out[i])
            if new_in != live_in[i]:
                live_in[i] = new_in
                changed = True

    # -- remove dead stores using the computed live_out of each block -----
    dead_removed = 0
    new_blocks: List[List[Instr]] = []
    for i, block in enumerate(blocks):
        if i not in reachable:
            new_blocks.append(keep_decls[i])
            continue
        live = set(live_out[i])
        kept_reversed: List[Instr] = []
        for instr in reversed(block):
            dest, uses = _def_use(instr)
            if (dest and instr.op in ("ASSIGN", "BIN", "UN", "CAST")
                    and dest not in live and dest not in globals_):
                if may_fault(instr):
                    _note_kept(skipped, instr, dest)       # keep: removing it could hide a crash
                    live.discard(dest)
                    live |= uses
                    kept_reversed.append(instr)
                    continue
                dead_removed += 1
                if not _is_temp(dest):
                    events.append(OptEvent(instr.line, "dead-store",
                        f"value assigned to '{_display(dest)}' is never used; assignment removed"))
                continue
            if dest:
                live.discard(dest)
            live |= uses
            kept_reversed.append(instr)
        new_blocks.append(list(reversed(kept_reversed)))

    flat: List[Instr] = []
    for blk in new_blocks:
        flat.extend(blk)
    return flat, dead_removed, unreachable_removed


def _prune_labels(instrs: List[Instr]) -> List[Instr]:
    targets = {x.dest for x in instrs if x.op in ("GOTO", "IF_FALSE")}
    # `L_forcont*` marks where a `for` loop's body ends and its update begins;
    # it is kept even with no `continue` so the C emitter can still tell the
    # two apart and write the loop back as `for (...; ...; update)`.
    return [x for x in instrs
            if not (x.op == "LABEL" and x.dest not in targets
                    and not x.dest.startswith("L_forcont"))]


def _drop_goto_to_next(instrs: List[Instr]) -> List[Instr]:
    out = []
    for i, x in enumerate(instrs):
        if x.op == "GOTO" and i + 1 < len(instrs) and instrs[i + 1].op == "LABEL" \
                and instrs[i + 1].dest == x.dest:
            continue
        out.append(x)
    return out


# -- pass 3b: useless-code elimination (self-referential dead stores) ------
def _is_observable_instr(instr: Instr, globals_: Set[str], may_fault=None) -> bool:
    """True for an instruction whose effect can be observed from
    outside the function's own dead-end local variables: control flow
    (kept untouched here -- removing it is `dead_code_elimination`'s
    job, not this pass's), a function call (may have side effects this
    module can't see into, e.g. I/O -- same assumption the rest of the
    optimiser already makes about CALL), returning a value, or a write
    this module already treats as "could be observed elsewhere" (a
    global, or anything indexed/dereferenced/member-accessed, since
    those may alias memory reachable through a pointer another function
    holds)."""
    if instr.op in ("LABEL", "GOTO", "IF_FALSE", "RETURN", "CALL"):
        return True
    if instr.op == "DECL":
        return instr.arg2 is not None
    if may_fault is not None and may_fault(instr):
        return True
    dest = instr.dest
    if not dest or not isinstance(dest, str):
        return False
    if dest in globals_:
        return True
    return _split_indexed(dest) is not None or _is_unsafe_write(dest)


def _useless_code_closure(instrs: List[Instr], globals_: Set[str], may_fault=None) -> Set[int]:
    """Indices of instructions that must be kept: every instruction
    with an externally-observable effect, plus (transitively) every
    instruction whose result one of those depends on.

    This is strictly more powerful than the backward liveness analysis
    `dead_code_elimination` already does for straight-line/branching
    dead stores: liveness alone cannot see that a variable is dead when
    its *only* reader is itself, e.g. `unused = unused + i;` inside a
    loop -- `unused` is "live" at that point by liveness (it's about to
    be read right there), and that live range never ends because the
    same instruction is also where it's defined, so the cycle looks
    perpetually live even though nothing outside the loop ever reads
    `unused`'s final value. Tracing necessity from actual observable
    effects instead (a small "mark the dependency graph, sweep what's
    unmarked" pass -- the same idea backward liveness already uses, one
    level more global) breaks that cycle correctly."""
    necessary = [False] * len(instrs)
    # name -> every instruction index that (re)defines it in the plain,
    # trackable sense (not an array/pointer/member write -- those are
    # always necessary already via `_is_observable_instr`, so they
    # never need to be *reached* through this map).
    defs_by_name: Dict[str, List[int]] = {}
    for idx, instr in enumerate(instrs):
        dest, _ = _def_use_raw(instr)
        if dest and _split_indexed(dest) is None and not _is_unsafe_write(dest):
            defs_by_name.setdefault(dest, []).append(idx)

    worklist: List[int] = []
    for idx, instr in enumerate(instrs):
        if _is_observable_instr(instr, globals_, may_fault):
            necessary[idx] = True
            worklist.append(idx)

    while worklist:
        idx = worklist.pop()
        instr = instrs[idx]
        _, uses = _def_use_raw(instr)
        dest = instr.dest
        if dest and (_split_indexed(dest) is not None or _is_unsafe_write(dest)):
            # the store's own address/index expression must stay
            # available too (`a[i] = v` needs `i`; `p->f = v` needs `p`)
            uses = uses | _extract_uses(dest)
        for name in uses:
            for def_idx in defs_by_name.get(name, ()):
                if not necessary[def_idx]:
                    necessary[def_idx] = True
                    worklist.append(def_idx)

    return {i for i, v in enumerate(necessary) if v}


def useless_code_elimination(instrs: List[Instr], globals_: Set[str] = frozenset(),
                             events: Optional[List[OptEvent]] = None,
                             may_fault=None) -> Tuple[List[Instr], int]:
    """Removes a plain-variable store (`ASSIGN`/`BIN`/`UN`/`CAST` with
    an ordinary name as `dest`) that `_useless_code_closure` found
    unreachable from any observable effect. Control flow, calls,
    globals, and array/pointer/member writes are never touched here --
    see `_is_observable_instr`."""
    events = events if events is not None else []
    necessary = _useless_code_closure(instrs, globals_, may_fault)
    out: List[Instr] = []
    removed = 0
    for idx, instr in enumerate(instrs):
        if idx in necessary or instr.op not in ("ASSIGN", "BIN", "UN", "CAST") or not instr.dest:
            out.append(instr)
            continue
        removed += 1
        if not _is_temp(instr.dest):
            events.append(OptEvent(instr.line, "dead-store",
                f"value assigned to '{_display(instr.dest)}' is never used; assignment removed"))
    return out, removed


def _drop_unused_decls(instrs: List[Instr], globals_: Set[str]) -> List[Instr]:
    """A variable whose every use was optimised away no longer needs a
    declaration. (Done inside the fixed-point loop, so if the dropped
    declaration carried an array initialiser, the temporaries feeding
    it become dead and are removed on the next iteration.)"""
    mentioned = set()
    for x in instrs:
        if x.op == "DECL":
            mentioned |= {_base_name(u) for u in _extract_uses(x.arg2)}
            continue
        for a in (x.arg1, x.arg2, x.dest):
            mentioned |= {_base_name(u) for u in _extract_uses(a)}
            if isinstance(a, str):
                mentioned.add(_base_name(a))
    return [x for x in instrs
            if not (x.op == "DECL" and x.dest not in mentioned and x.dest not in globals_)]


# -- driver -----------------------------------------------------------
MAX_ITERATIONS = 30


def optimize(instrs: List[Instr], globals_: Optional[Set[str]] = None, *,
             global_types: Optional[Dict[str, str]] = None,
             global_arrays: Optional[Dict[str, Tuple[str, int]]] = None,
             param_types: Optional[Dict[str, str]] = None,
             func_returns: Optional[Dict[str, str]] = None
             ) -> Tuple[List[Instr], OptimizationStats]:
    """Runs the passes to a fixed point. `globals_` names variables that
    outlive the function (other functions may read them), so stores to
    them are never treated as dead. The keyword arguments describe the
    types the passes need (see OptContext); without them the passes
    still run but refuse any rewrite that depends on an unknown type.

    Iteration stops when a whole round changes nothing. A round cap
    (MAX_ITERATIONS) guards against a pass pair that keeps undoing each
    other's work; `stats.converged` records whether the cap was hit."""
    globals_ = set(globals_ or ())
    stats = OptimizationStats()
    ctx = OptContext(globals_, global_types, global_arrays, param_types, func_returns)
    current = instrs
    stats.converged = False

    for _ in range(MAX_ITERATIONS):
        before = [str(x) for x in current]
        stats.passes_run += 1

        ctx.refresh(current)
        current, f = constant_folding(current, ctx, stats.events, stats.skipped)
        ctx.refresh(current)
        current, alg = algebraic_simplification(current, ctx, stats.events, stats.skipped)
        ctx.refresh(current)
        current, r = common_subexpression_elimination(current, ctx, stats.events)
        ctx.refresh(current)
        current, c = copy_propagation(current, ctx, stats.events)
        ctx.refresh(current)
        current, ve = eliminate_single_use_variables(current, ctx, stats.events)
        ctx.refresh(current)
        current, pa = simplify_pointer_arithmetic(current, ctx, stats.events)
        ctx.refresh(current)
        current, sa = remove_redundant_self_assignments(current, ctx, stats.events, stats.skipped)
        # Liveness only sees names. A variable whose address was taken can
        # also be read through a pointer (`*p`) or by a callee, so a store
        # to it is never "dead" -- treat it like a global from here on.
        ctx.refresh(current)
        fault = lambda ins, _c=ctx: _may_fault(ins, _c)       # noqa: E731
        current, d, u = dead_code_elimination(current, ctx.escaped, stats.events,
                                              stats.skipped, fault)
        current, uc = useless_code_elimination(current, ctx.escaped, stats.events, fault)
        current = _drop_goto_to_next(_prune_labels(current))
        current = _drop_unused_decls(current, globals_)
        stats.constants_folded += f
        stats.algebraic_simplifications += alg
        stats.subexpressions_reused += r
        stats.copies_propagated += c
        stats.variables_eliminated += ve
        stats.pointer_simplifications += pa
        stats.dead_stores_removed += d + sa + uc
        stats.unreachable_instructions_removed += u
        if [str(x) for x in current] == before:
            stats.converged = True
            break

    current = _drop_unused_decls(current, globals_)

    stats.events = _dedupe(stats.events)
    stats.skipped = _dedupe(stats.skipped)
    stats.events.sort(key=lambda e: e.line)
    stats.skipped.sort(key=lambda e: e.line)
    return current, stats


def _dedupe(events: List[OptEvent]) -> List[OptEvent]:
    """Skipped/detected findings are re-discovered on every round."""
    seen = set()
    out = []
    for e in events:
        key = (e.line, e.kind, e.message, e.status)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def optimize_program(global_instrs: List[Instr], functions
                     ) -> Tuple[List[Instr], List[Tuple[str, OptimizationStats]]]:
    """Optimises the global initialisers and every function body (in
    place on `functions`), giving each pass the declared types of globals,
    parameters and function return values. Returns the optimised globals
    and a [(name, stats)] list, "<globals>" first."""
    global_names = {i.dest for i in global_instrs if i.op == "DECL"}
    global_types: Dict[str, str] = {}
    global_arrays: Dict[str, Tuple[str, int]] = {}
    for ins in global_instrs:
        if ins.op == "DECL":
            if ins.arg1 is not None:
                dims = ins.arg1 if isinstance(ins.arg1, list) else [ins.arg1]
                global_arrays[ins.dest] = (ins.extra, len(dims))
            else:
                global_types[ins.dest] = ins.extra
    func_returns = {fn.name: fn.return_type for fn in functions}
    kw = dict(global_types=global_types, global_arrays=global_arrays, func_returns=func_returns)

    global_opt, g_stats = optimize(global_instrs, global_names, **kw)
    all_stats = [("<globals>", g_stats)]
    for fn in functions:
        fn.instructions, stats = optimize(fn.instructions, global_names,
                                          param_types=fn.param_types, **kw)
        all_stats.append((fn.name, stats))
    return global_opt, all_stats
