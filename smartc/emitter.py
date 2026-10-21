"""
C Code Emitter
--------------
Turns the (optimised) three-address code back into readable C so the
user can see the effect of the optimisation passes on actual source,
as the project proposal's expected output shows.

Three-address code has one operation per instruction and uses
compiler temporaries (t0, t1, ...). To get back to natural C, a
temporary that is written once and read once, with nothing in between
that could change its operands, is folded back into the expression
that uses it (`t0 = x * 4; y = t0;` becomes `y = x * 4;`).

Control flow: the optimiser keeps `if` / `while` / `for` lowered to
labels and jumps internally.  This module only produces the *statements*
(as `Item`s); `structurer.py` then rebuilds `if`/`else`/`while`/`for`/
`break`/`continue` from them, and falls back to `if (!c) goto L;` /
`goto L;` / `L:` for anything it cannot prove it can re-spell exactly.
"""

import re
from typing import List, Dict, Optional

from .ir import Instr, IRFunction
from .structurer import Item, structure, render_flat
from .ast_nodes import Program, FuncDecl, StructDecl, Block, If, While, For
from .optimizer import (
    _extract_uses, _is_temp, _display, _split_indexed, _is_unsafe_write,
    SIZEOF_RE, OptContext,
)

TYPE_KEYWORD_BASES = {"int", "float", "char", "double", "void"}

STDIO_FUNCS = {"printf", "scanf", "fprintf", "sprintf", "puts", "putchar", "getchar", "gets"}
STDLIB_FUNCS = {"malloc", "calloc", "realloc", "free"}
STRING_FUNCS = {"strlen"}

# precedence used to decide where parentheses are required
PREC = {"||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, ">": 4, "<=": 4, ">=": 4,
        "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}
ATOM = 99

# relational/equality operators do not associate with each other: `a <= b`
# used as the left side of `>= c` is numerically equivalent whether or not
# it is parenthesised, but writing it bare (`a <= b >= c`) is flagged by
# clang as a chained comparison and rejected even with warnings suppressed
# (-w does not silence it), so this module always parenthesises a
# comparison that itself feeds another comparison at the same precedence.
COMPARISON_PREC = {"==", "!=", "<", ">", "<=", ">="}


# IR names (alpha-renamed, e.g. `x__2`) that must be emitted verbatim in
# the function currently being emitted: stripping the suffix would give
# two different variables the same C name in one flat function scope.
# Set per function by `emit_function` (and for the signature).
_KEEP_SUFFIX: set = set()
_RENAMED_TOKEN_RE = re.compile(r"[A-Za-z_]\w*__\d+")


def _name(x) -> str:
    return _RENAMED_TOKEN_RE.sub(
        lambda m: m.group(0) if m.group(0) in _KEEP_SUFFIX else _display(m.group(0)), str(x))


def _names_needing_suffix(fn: IRFunction, global_names) -> set:
    """IR names in `fn` whose display name (suffix stripped) would
    collide with a different IR name also used in `fn` (another local,
    a parameter, or a global the function reads)."""
    seen = set(global_names)
    for p in fn.params:
        seen.add(p.split()[-1])
    for ins in fn.instructions:
        for a in (ins.dest, ins.arg1, ins.arg2):
            for item in (a if isinstance(a, list) else [a]):
                if isinstance(item, list):
                    for sub in item:
                        seen.update(re.findall(r"[A-Za-z_]\w*", str(sub)))
                elif item is not None:
                    seen.update(re.findall(r"[A-Za-z_]\w*", str(item)))
    groups: Dict[str, set] = {}
    for name in seen:
        groups.setdefault(_display(name), set()).add(name)
    keep = set()
    for names in groups.values():
        if len(names) > 1:
            keep |= {n for n in names if n != _display(n)}
    return keep


def _array_size_suffix(arg1) -> str:
    if isinstance(arg1, list):
        return "".join(f"[{d}]" for d in arg1)
    if arg1 is not None:
        return f"[{arg1}]"
    return ""


def _split_mixed_chain(s: str):
    """Decomposes an lvalue-chain operand string -- any mix of
    indexing and struct/union member access in any order, e.g.
    'a[i][j]', 'p->x', 's.vals[0]', 'boxes[i].vals[j]', 'p->next->val'
    -- into (base_name, ops), where each op is either
    ('index', index_text) or ('member', '.'|'->', member_name).

    `_split_indexed` and `MEMBER_RE` (in optimizer.py) each only
    recognise ONE of these two kinds of step on their own; a mix (a
    struct field that's an array, or an array of structs with its own
    array field) needs them interleaved in the right order, which is
    what this does. Returns None if `s` doesn't start with a plain
    identifier at all (a literal, a temp name, ...)."""
    m = re.match(r"^[A-Za-z_]\w*", s)
    if not m:
        return None
    name = m.group(0)
    i, n = m.end(), len(s)
    ops = []
    while i < n:
        if s[i] == "[":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if s[j] == "[":
                    depth += 1
                elif s[j] == "]":
                    depth -= 1
                j += 1
            if depth != 0:
                return None
            ops.append(("index", s[i + 1:j - 1]))
            i = j
        elif s[i] == ".":
            mm = re.match(r"\.([A-Za-z_]\w*)", s[i:])
            if not mm:
                return None
            ops.append(("member", ".", mm.group(1)))
            i += mm.end()
        elif s[i:i + 2] == "->":
            mm = re.match(r"->([A-Za-z_]\w*)", s[i:])
            if not mm:
                return None
            ops.append(("member", "->", mm.group(1)))
            i += mm.end()
        else:
            return None
    return name, ops


def _operand(x) -> str:
    if x is None:
        return ""
    s = str(x)
    if s.startswith("*"):
        return f"*{_operand(s[1:])}"
    chain = _split_mixed_chain(s)
    if chain and chain[1]:
        name, ops = chain
        cur = _operand(name)
        for kind, *rest in ops:
            if kind == "index":
                cur = f"{cur}[{_operand(rest[0])}]"
            else:
                sep, member = rest
                cur = f"{cur}{sep}{member}"
        return cur
    return _name(s)


def _touches_memory(operand) -> bool:
    """An operand that reads through a pointer, an index or a member."""
    if operand is None:
        return False
    text = str(operand)
    if text[:1] in ("'", '"'):
        return False
    return text.startswith("*") or "[" in text or "->" in text or "." in re.sub(r"\d+\.\d*", "", text)


class _Expr:
    def __init__(self, text: str, prec: int = ATOM):
        self.text = text
        self.prec = prec

    def wrap(self, min_prec: int) -> str:
        return self.text if self.prec >= min_prec else f"({self.text})"


_COMPOUND_ASSIGNABLE_OPS = ("+", "-", "*", "/", "%")


def _compound_or_increment(target: str, extra: str, a: str, b: str) -> Optional[str]:
    """`target = target <extra> b;` (or, for the two commutative
    operators `+`/`*`, `target = a <extra> target;`) is exactly
    `target <extra>= b;` (respectively `a`) in C -- and adding/
    subtracting the literal `1` this way is `target++`/`target--`.
    Returns that equivalent statement text (with its trailing `;`), or
    None if this isn't one of those shapes.

    `a` and `b` must be the *immediate*, already fully-rendered operand
    texts of the single BIN instruction that directly produced this
    value (see the two call sites: either the current instruction IS
    that BIN, or it is one single-use temporary away from it, tracked
    through `pending_bin`) -- never a flattened string that might
    itself be the result of further chained same-precedence operators.
    Checking `a == target` / `b == target` as whole-string equality
    (not a prefix/suffix regex on a merged expression) is what makes
    this sound: `arr[idx] = (arr[idx] % 31) * (b % 31) % 100` must NOT
    become `arr[idx] %= 31 * (b % 31) % 100` (wrong -- modulo/
    multiplication chain left-associates, so the outermost operation
    here is the trailing `% 100`, not a `%` with `arr[idx]` as a direct
    operand), and comparing whole strings correctly rejects it, since
    the outer BIN's own immediate operands are the *whole* inner
    product expression and `100` -- neither of which equals `arr[idx]`
    -- rather than merely starting with that text.

    This never double-evaluates anything the plain form did not: this
    IR always computes a side-effecting sub-expression (a call, an
    increment) into its own temporary before it is ever used as an
    operand, so an operand reaching here is always a plain
    already-evaluated name/constant/memory read."""
    if extra not in _COMPOUND_ASSIGNABLE_OPS:
        return None
    if a == target:
        other = b
    elif extra in ("+", "*") and b == target:
        other = a
    else:
        return None
    # `*p++;` means `*(p++)` in C -- postfix `++`/`--` bind tighter than
    # a prefix dereference, so the shorthand is only safe for a target
    # with no leading `*` (a plain name, or one ending in `[...]` /
    # `.member` / `->member`, all of which bind at the same tight
    # postfix precedence as `++`/`--` itself and so group correctly:
    # `arr[i]++` really is `(arr[i])++`). `+=`/`-=` have no such hazard
    # -- assignment binds looser than every unary and postfix operator,
    # so `*p += 1;` is always `(*p) += 1;`.
    if other == "1" and extra in ("+", "-") and not target.startswith("*"):
        return f"{target}{'++' if extra == '+' else '--'};"
    return f"{target} {extra}= {other};"


def _struct_field_types(program: Optional[Program]) -> Dict[str, Dict[str, str]]:
    """struct/union tag -> {field name -> declared C type}, used to infer
    the type of a compiler temporary loaded from a member access (`s.f`,
    `p->f`) when `OptContext.type_of` can't see through it on its own
    (it has no model of struct layout)."""
    result: Dict[str, Dict[str, str]] = {}
    if program is None:
        return result
    for sd in _collect_struct_decls(program):
        result[sd.tag] = {f.name: f.type for f in sd.fields}
    return result


def _chain_type(ctx: "OptContext", struct_types: Dict[str, Dict[str, str]],
                name: str, ops) -> Optional[str]:
    t = ctx.type_of(name)
    for kind, *rest in ops:
        if t is None:
            return None
        if kind == "index":
            t = t[:-1] if t.endswith("*") else None
        else:
            sep, member = rest
            if sep == "->":
                if not t.endswith("*"):
                    return None
                t = t[:-1]
            if not t.startswith(("struct ", "union ")):
                return None
            fields = struct_types.get(t.split(None, 1)[1])
            t = fields.get(member) if fields else None
    return t


def _infer_type(ctx: "OptContext", struct_types: Dict[str, Dict[str, str]], operand) -> str:
    """The C type to declare a compiler temporary with, as close as this
    tool's type model gets. Falls back to a struct-aware chain walk for
    a temp loaded from a member access (`OptContext.type_of` has no
    notion of struct fields), to `unsigned long` for `sizeof(...)`
    (always returns `size_t`), and to `int` only if every other source
    of information is exhausted (should not happen for code this tool
    itself generated, but keeps emission total rather than crashing)."""
    s = str(operand)
    t = ctx.type_of(s)
    if t is not None:
        return t
    if s.startswith("*"):
        inner = _infer_type(ctx, struct_types, s[1:])
        if inner.endswith("*"):
            return inner[:-1]
    elif SIZEOF_RE.match(s):
        return "unsigned long"
    else:
        chain = _split_mixed_chain(s)
        if chain and chain[1]:
            name, ops = chain
            t = _chain_type(ctx, struct_types, name, ops)
            if t is not None:
                return t
    return "int"


def emit_function(fn: IRFunction, ctx: "OptContext", struct_types: Dict[str, Dict[str, str]],
                  structured: bool = True) -> List[str]:
    instrs = fn.instructions

    # use counts of every name across the function
    uses: Dict[str, int] = {}

    def count_reads(operand):
        """Every textual occurrence counts: `f(t, t)` reads t twice, so t
        must not be inlined at its first use and left dangling at the second
        (a set of names, as `_extract_uses` returns, would count it once)."""
        if operand is None:
            return
        if isinstance(operand, list):
            for item in operand:
                count_reads(item)
            return
        text = str(operand)
        if text[:1] in ("'", '"'):
            return
        for tok in re.findall(r"[A-Za-z_]\w*", text):
            if tok not in TYPE_KEYWORD_BASES:
                uses[tok] = uses.get(tok, 0) + 1

    for ins in instrs:
        count_reads(ins.arg1)
        count_reads(ins.arg2)
        if (ins.op in ("ASSIGN", "BIN", "UN", "CALL", "CAST") and ins.dest
                and isinstance(ins.dest, str)
                and (_split_indexed(ins.dest) is not None or _is_unsafe_write(ins.dest))):
            count_reads(ins.dest)

    # a temporary written in more than one place (the result of a lowered
    # `&&` / `||`) must stay a real variable: it is declared at its first
    # definition and assigned at the others, never inlined at a use
    defs: Dict[str, int] = {}
    for ins in instrs:
        if (ins.op in ("ASSIGN", "BIN", "UN", "CALL", "CAST") and isinstance(ins.dest, str)
                and _is_temp(ins.dest)):
            defs[ins.dest] = defs.get(ins.dest, 0) + 1
    declared_temps = set()

    pending: Dict[str, _Expr] = {}       # temp -> expression text
    # for a pending temp defined by exactly one BIN: (op, a, b), the
    # *immediate* already-rendered operand texts of that one BIN -- see
    # `_compound_or_increment`. Kept in lockstep with `pending`: popped
    # wherever a pending entry is popped or force-materialised, so a
    # temp that got merged into a larger expression (no longer standing
    # for just that one BIN) or flushed to its own statement (no longer
    # standing for a live, reassignable "target" at all) never leaves a
    # stale entry a later statement could wrongly match against.
    pending_bin: Dict[str, Tuple[str, str, str]] = {}
    # pending temps whose expression reads memory (`*p`, `a[i]`, `p->f`, ...).
    # Any write to a named variable can change such a read through an alias
    # (`int *p = &b; ... b = 2;`), so these are materialised before one.
    reads_memory: set = set()
    consumed_memory = [False]
    # variables read by the pending temps that render() has inlined into the
    # expression currently being built (dependencies must be transitive)
    consumed_deps: List[set] = [set()]
    pending_deps: Dict[str, set] = {}    # temp -> variables its value depends on
    lines: List[Item] = []
    # (index in `lines`, C type text, variable name) of the declaration that
    # was emitted last, if nothing has been emitted since -- `int x;` followed
    # directly by `x = e;` is written as `int x = e;`
    open_decl: List = [None]

    def add(text, decl=None, expr=False):
        lines.append(Item("stmt", text, decl=decl, expr=expr))

    def render(x) -> _Expr:
        if x is None:
            return _Expr("")
        s = str(x)
        if s in pending:
            e = pending.pop(s)
            consumed_deps[0] |= pending_deps.pop(s, set())
            pending_bin.pop(s, None)
            if s in reads_memory:
                reads_memory.discard(s)
                consumed_memory[0] = True
            return e
        if s.startswith("*"):
            inner = render(s[1:])
            return _Expr(f"*{inner.wrap(ATOM)}", 7)
        m = SIZEOF_RE.match(s)
        if m:
            inner_s = m.group(1)
            if inner_s.rstrip("*") in TYPE_KEYWORD_BASES:
                return _Expr(f"sizeof({inner_s})", ATOM)
            inner = render(inner_s)
            return _Expr(f"sizeof({inner.text})", ATOM)
        chain = _split_mixed_chain(s)
        if chain and chain[1]:
            name, ops = chain
            cur = render(name)
            for kind, *rest in ops:
                if kind == "index":
                    idx = render(rest[0])
                    cur = _Expr(f"{cur.wrap(ATOM)}[{idx.text}]", ATOM)
                else:
                    sep, member = rest
                    cur = _Expr(f"{cur.wrap(ATOM)}{sep}{member}", ATOM)
            return cur
        return _Expr(_operand(s))

    def flush_dependents(written: str):
        """A write invalidates pending expressions that read the written
        variable; they must be materialised before the write happens."""
        base = written.split("[")[0]
        aliasable = not _is_temp(written)
        for t in [t for t, deps in pending_deps.items()
                  if base in deps or (aliasable and t in reads_memory)]:
            e = pending.pop(t)
            pending_deps.pop(t)
            pending_bin.pop(t, None)
            reads_memory.discard(t)
            add(f"{_infer_type(ctx, struct_types, t)} {t} = {e.text};", decl=t)

    def deps_of(*operands) -> set:
        d = set()
        for o in operands:
            d |= {u.split("[")[0] for u in _extract_uses(o)}
        return d

    def flush_all():
        for t in list(pending):
            e = pending.pop(t)
            pending_deps.pop(t, None)
            pending_bin.pop(t, None)
            reads_memory.discard(t)
            add(f"{_infer_type(ctx, struct_types, t)} {t} = {e.text};", decl=t)

    for idx, ins in enumerate(instrs):
        op = ins.op
        if op == "LABEL":
            flush_all()
            lines.append(Item("label", target=_name(ins.dest)))
        elif op == "GOTO":
            flush_all()
            lines.append(Item("goto", target=_name(ins.dest)))
        elif op == "IF_FALSE":
            c = render(ins.arg1)
            flush_all()
            lines.append(Item("iffalse", c.text, target=_name(ins.dest), prec=c.prec))
        elif op == "RETURN":
            if ins.arg1 is None:
                flush_all()
                add("return;")
            else:
                e = render(ins.arg1)
                flush_all()
                add(f"return {e.text};")
        elif op == "DECL":
            size = _array_size_suffix(ins.arg1)
            if ins.arg2 is not None:
                init = _render_init(ins.arg2, lambda x: render(x).text)
                add(f"{ins.extra} {_name(ins.dest)}{size} = {init};", decl=_name(ins.dest))
            else:
                add(f"{ins.extra} {_name(ins.dest)}{size};", decl=_name(ins.dest))
                if not size:
                    open_decl[0] = (len(lines) - 1, ins.extra, _name(ins.dest))
        elif op in ("BIN", "UN", "ASSIGN", "CALL", "CAST"):
            consumed_memory[0] = False
            consumed_deps[0] = set()
            # (extra, a, b): the *immediate* operand texts of the single
            # BIN this value reduces to (itself, or one single-use temp
            # away from it, via `pending_bin`) -- see
            # `_compound_or_increment`. None for every other shape.
            compound_src: Optional[Tuple[str, str, str]] = None
            if op == "BIN":
                prec = PREC.get(ins.extra, 1)
                left_min = prec + 1 if ins.extra in COMPARISON_PREC else prec
                a = render(ins.arg1).wrap(left_min)
                b = render(ins.arg2).wrap(prec + 1)
                rhs = _Expr(f"{a} {ins.extra} {b}", prec)
                compound_src = (ins.extra, a, b)
            elif op == "UN":
                a = render(ins.arg1).wrap(ATOM)
                rhs = _Expr(f"{ins.extra}{a}", 7)
            elif op == "CAST":
                a = render(ins.arg1).wrap(ATOM)
                rhs = _Expr(f"({ins.extra}){a}", 7)
            elif op == "ASSIGN":
                if isinstance(ins.arg1, str):
                    compound_src = pending_bin.get(ins.arg1)
                rhs = render(ins.arg1)
            else:
                args = ", ".join(render(a).text for a in (ins.arg1 or []))
                rhs = _Expr(f"{ins.extra}({args})", ATOM)

            if op == "CALL":
                # a call may mutate anything reachable through a pointer
                # it was handed, or any global -- nothing still pending
                # can safely be deferred across it.
                flush_all()

            dest = ins.dest
            if dest and _is_temp(dest) and defs.get(dest, 0) > 1:
                flush_dependents(dest)
                prefix = "" if dest in declared_temps else f"{_infer_type(ctx, struct_types, dest)} "
                declared_temps.add(dest)
                add(f"{prefix}{dest} = {rhs.text};", decl=None if not prefix else dest,
                    expr=not prefix)
            elif dest and _is_temp(dest):
                single_use = uses.get(dest, 0) == 1
                # calls have side effects: only inline if used right away
                # a call has side effects, so it may only be inlined into the
                # very next instruction (nothing can be reordered around it)
                next_uses = idx + 1 < len(instrs) and dest in (
                    _extract_uses(instrs[idx + 1].arg1) | _extract_uses(instrs[idx + 1].arg2))
                if single_use and (op != "CALL" or next_uses):
                    pending[dest] = rhs
                    pending_deps[dest] = deps_of(ins.arg1, ins.arg2) | consumed_deps[0]
                    if compound_src is not None:
                        pending_bin[dest] = compound_src
                    operands = [ins.arg1, ins.arg2] if op != "CALL" else list(ins.arg1 or [])
                    if (consumed_memory[0] or (op == "UN" and ins.extra == "*")
                            or any(_touches_memory(o) for o in operands)):
                        reads_memory.add(dest)
                elif uses.get(dest, 0) == 0:
                    # a call is always evaluated; any other unused temporary that
                    # reached here was kept because it can fault (x / y, *p)
                    if op == "CALL":
                        add(f"{rhs.text};", expr=True)
                    else:
                        # `(void)(a / b);` is simply dropped by the C compiler, which
                        # would turn a crash into a success; storing the value keeps it
                        add(f"{_infer_type(ctx, struct_types, dest)} {dest} = {rhs.text};", decl=dest)
                        add(f"(void){dest};")
                else:
                    flush_dependents(dest)
                    add(f"{_infer_type(ctx, struct_types, dest)} {dest} = {rhs.text};", decl=dest)
            elif dest and isinstance(dest, str) and _is_unsafe_write(dest):
                # a write through a pointer, or to a struct/union
                # member, may alias anything -- flush everything
                # pending first, same reasoning as a CALL.
                flush_all()
                tgt = render(dest).text
                compound = (_compound_or_increment(tgt, *compound_src)
                           if compound_src is not None else None)
                add(compound or f"{tgt} = {rhs.text};", expr=True)
            elif dest:
                flush_dependents(dest)
                target = render(dest).text
                od = open_decl[0]
                if (od is not None and od[0] == len(lines) - 1 and od[2] == target
                        and not re.search(rf"\b{re.escape(target)}\b", rhs.text)):
                    lines[od[0]].text = f"{od[1]} {target} = {rhs.text};"
                    open_decl[0] = None
                else:
                    compound = (_compound_or_increment(target, *compound_src)
                               if compound_src is not None else None)
                    add(compound or f"{target} = {rhs.text};", expr=True)
            else:
                add(f"{rhs.text};", expr=True)
    flush_all()
    if not structured:
        return render_flat(lines)
    rendered, _info = structure(lines)
    return rendered


def _render_init(init, render_one) -> str:
    """A DECL initialiser -> C text: nested lists become nested braces
    (an empty list is `{0}`, which zero-initialises every element), a
    string literal is used as is."""
    if isinstance(init, list):
        if not init:
            return "{0}"
        return "{" + ", ".join(_render_init(x, render_one) for x in init) + "}"
    return render_one(init)


def _needed_includes(global_instrs: List[Instr], functions: List[IRFunction]) -> List[str]:
    """The optimised IR only ever calls out to library functions this
    tool itself recognised at the semantic stage (malloc & co, basic
    stdio) -- scanning for those calls (and any literal NULL) lets the
    emitted C stand alone and actually compile, without a real
    preprocessor to have carried the original #include lines through."""
    needs_stdio = needs_stdlib = needs_string = needs_null = False
    all_instrs = list(global_instrs)
    for fn in functions:
        all_instrs.extend(fn.instructions)
    for ins in all_instrs:
        if ins.op == "CALL":
            if ins.extra in STDIO_FUNCS:
                needs_stdio = True
            if ins.extra in STDLIB_FUNCS:
                needs_stdlib = True
            if ins.extra in STRING_FUNCS:
                needs_string = True
        for operand in (ins.arg1, ins.arg2):
            values = operand if isinstance(operand, list) else [operand]
            if any(v == "NULL" for v in values):
                needs_null = True

    includes = []
    if needs_stdio:
        includes.append("#include <stdio.h>")
    if needs_stdlib:
        includes.append("#include <stdlib.h>")
    if needs_string:
        includes.append("#include <string.h>")
    if needs_null and not (needs_stdio or needs_stdlib):
        includes.append("#include <stddef.h>")
    return includes


def _collect_struct_decls(program: Program) -> List[StructDecl]:
    """Struct/union tag declarations aren't lowered into the IR at all
    (see ir.py) -- they're a pure type-system fact, so they're walked
    straight off the AST here instead, wherever they appear (top level
    or local to a function body), and re-emitted verbatim up front."""
    found: List[StructDecl] = []

    def walk_stmt(node):
        if node is None:
            return
        if isinstance(node, StructDecl):
            found.append(node)
        elif isinstance(node, Block):
            for s in node.statements:
                walk_stmt(s)
        elif isinstance(node, If):
            walk_stmt(node.then_branch)
            walk_stmt(node.else_branch)
        elif isinstance(node, While):
            walk_stmt(node.body)
        elif isinstance(node, For):
            walk_stmt(node.init)
            walk_stmt(node.body)

    for decl in program.declarations:
        if isinstance(decl, StructDecl):
            found.append(decl)
        elif isinstance(decl, FuncDecl) and decl.body is not None:
            walk_stmt(decl.body)
    return found


def emit_struct_defs(struct_decls: List[StructDecl]) -> List[str]:
    out: List[str] = []
    for sd in struct_decls:
        kw = "union" if sd.is_union else "struct"
        out.append(f"{kw} {sd.tag} {{")
        for f in sd.fields:
            size = "".join(f"[{d}]" for d in (f.array_dims or []))
            out.append(f"    {f.type} {f.name}{size};")
        out.append("};")
    if out:
        out.append("")
    return out


def _global_expr(operand, global_instrs: List[Instr], depth: int = 0) -> str:
    """Renders a file-scope initialiser operand as one C expression.
    Statements aren't allowed at file scope, so a temporary the
    optimiser couldn't fold away (e.g. `sizeof(int) * 2`) is expanded
    back into the expression that defined it, instead of being
    referenced by a name that would never be declared."""
    s = str(operand)
    if depth < 50 and _is_temp(s):
        for ins in global_instrs:
            if ins.dest == s:
                if ins.op == "ASSIGN":
                    return _global_expr(ins.arg1, global_instrs, depth + 1)
                if ins.op == "BIN":
                    return (f"({_global_expr(ins.arg1, global_instrs, depth + 1)} {ins.extra} "
                            f"{_global_expr(ins.arg2, global_instrs, depth + 1)})")
                if ins.op == "UN":
                    return f"{ins.extra}({_global_expr(ins.arg1, global_instrs, depth + 1)})"
                if ins.op == "CAST":
                    return f"({ins.extra})({_global_expr(ins.arg1, global_instrs, depth + 1)})"
    return _operand(s)


def emit_program(global_instrs: List[Instr], functions: List[IRFunction],
                  program: Optional[Program] = None) -> str:
    out: List[str] = []
    includes = _needed_includes(global_instrs, functions)
    if includes:
        out.extend(includes)
        out.append("")
    if program is not None:
        out.extend(emit_struct_defs(_collect_struct_decls(program)))
    decl_at: Dict[str, int] = {}
    for ins in global_instrs:
        if ins.op == "DECL":
            size = _array_size_suffix(ins.arg1)
            decl_at[ins.dest] = len(out)
            if ins.arg2 is not None:
                init = _render_init(ins.arg2, lambda x: _global_expr(x, global_instrs))
                out.append(f"{ins.extra} {_name(ins.dest)}{size} = {init};")
            else:
                out.append(f"{ins.extra} {_name(ins.dest)}{size};")
        elif ins.op == "ASSIGN" and ins.dest in decl_at:
            i = decl_at[ins.dest]
            out[i] = out[i][:-1] + f" = {_global_expr(ins.arg1, global_instrs)};"
    if decl_at:
        out.append("")
    global _KEEP_SUFFIX
    global_names = {ins.dest for ins in global_instrs if ins.op == "DECL"}
    global_types: Dict[str, str] = {}
    global_arrays: Dict[str, tuple] = {}
    for ins in global_instrs:
        if ins.op == "DECL":
            if ins.arg1 is not None:
                dims = ins.arg1 if isinstance(ins.arg1, list) else [ins.arg1]
                global_arrays[ins.dest] = (ins.extra, len(dims))
            else:
                global_types[ins.dest] = ins.extra
    func_returns = {fn.name: fn.return_type for fn in functions}
    struct_types = _struct_field_types(program)
    for fn in functions:
        _KEEP_SUFFIX = _names_needing_suffix(fn, global_names)
        params = ", ".join(f"{' '.join(p.split()[:-1])} {_name(p.split()[-1])}" for p in fn.params)
        sig = f"{fn.return_type} {fn.name}({params})"
        if not fn.has_body:
            out.append(sig + ";")
            continue
        out.append(sig + " {")
        if getattr(fn, "fold_notes", None):
            out.append("    /* Constant folding")
            out.extend(f"       {n}" for n in fn.fold_notes)
            out.append("    */")
        ctx = OptContext(global_names, global_types, global_arrays, fn.param_types, func_returns)
        ctx.refresh(fn.instructions)
        out.extend(emit_function(fn, ctx, struct_types))
        out.append("}")
        out.append("")
    _KEEP_SUFFIX = set()
    return "\n".join(out).rstrip() + "\n"
