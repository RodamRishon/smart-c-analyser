"""
Control-flow structuring for the C emitter
------------------------------------------
The optimiser works on a flat list of three-address instructions in
which every `if`, `while` and `for` has been lowered to labels and
jumps.  That form is convenient for analysis but it is not what a
person wrote, and it is not what they want to read.  This module is the
last step before text is produced: it takes the *already optimised*
statements for one function, as a flat list of `Item`s, and rebuilds
`if` / `if-else` / `while` / `for` / `break` / `continue` from the
label patterns the IR generator produces.

Nothing here changes what the program does.  Every rewrite is a pure
re-spelling of the same control flow, and the result is checked before
it is accepted:

* a jump whose label has been absorbed into a structured statement
  would be a dangling `goto` -- if any such jump is left, the whole
  function is emitted in the flat label/goto form instead;
* a statement is only wrapped in braces if nothing declared inside the
  braces is used after them (otherwise the declaration would go out of
  scope);
* anything that does not match a pattern exactly is simply left as a
  label / goto, so a program that cannot be fully structured still
  compiles and behaves the same.

The IR itself is never modified.
"""

import re
from typing import Dict, List, Optional, Set, Tuple

ATOM = 99

_LOOP_START_RE = re.compile(r"^L_(while|for)(\d+)$")
_TOKEN_RE = re.compile(r"[A-Za-z_]\w*")


class Item:
    """One emitted statement, before layout.

    kind   -- "stmt" | "label" | "goto" | "iffalse"
    text   -- C text of a statement (without indentation); for an
              "iffalse" item, the *positive* condition
    target -- label name for label / goto / iffalse
    prec   -- precedence of `text` for an "iffalse" condition
    decl   -- name this statement declares, if it is a declaration
    expr   -- True for a plain expression statement (`x = e;`, `f(a);`),
              which can be moved into a `for (...; ...; here)` header
    """
    __slots__ = ("kind", "text", "target", "prec", "decl", "expr", "_tokens")

    def __init__(self, kind, text="", target=None, prec=ATOM, decl=None, expr=False):
        self.kind = kind
        self.text = text
        self.target = target
        self.prec = prec
        self.decl = decl
        self.expr = expr
        self._tokens = None

    def tokens(self) -> Set[str]:
        if self._tokens is None:
            self._tokens = set(_TOKEN_RE.findall(self.text)) if self.kind in ("stmt", "iffalse") else set()
        return self._tokens


# --------------------------------------------------------------------------
# flat form (the original behaviour)
# --------------------------------------------------------------------------
def _negate(text: str, prec: int) -> str:
    if re.fullmatch(r"![A-Za-z_]\w*", text):
        return text[1:]
    return f"!{text}" if prec >= ATOM else f"!({text})"


def render_flat(items: List[Item]) -> List[str]:
    lines = []
    for it in items:
        if it.kind == "label":
            lines.append(f"{it.target}:;")
        elif it.kind == "goto":
            lines.append(f"    goto {it.target};")
        elif it.kind == "iffalse":
            lines.append(f"    if ({_negate(it.text, it.prec)}) goto {it.target};")
        else:
            lines.append(f"    {it.text}")
    return lines


# --------------------------------------------------------------------------
# structured nodes (tuples):
#   ("stmt", text, expr_flag)       ("label", name)       ("goto", name)
#   ("ifgoto", cond_text, name)     ("break",)            ("continue",)
#   ("if", cond, then, else_or_None)
#   ("while", cond_or_None, body)
#   ("for", init_or_None, cond_or_None, step_or_None, body)
# --------------------------------------------------------------------------
class _Structurer:
    def __init__(self, items: List[Item]):
        self.items = items
        n = len(items)
        self.label_at: Dict[str, int] = {}
        for i, it in enumerate(items):
            if it.kind == "label":
                self.label_at[it.target] = i
        # last position at which each identifier is mentioned
        self.last_use: Dict[str, int] = {}
        for i, it in enumerate(items):
            for tok in it.tokens():
                self.last_use[tok] = i
        self.n = n
        self.loops_built = 0
        self.ifs_built = 0

    # -- helpers -----------------------------------------------------------
    def _decls(self, lo: int, hi: int) -> List[str]:
        return [it.decl for it in self.items[lo:hi] if it.decl]

    def _scope_ok(self, lo: int, hi: int, region_end: int) -> bool:
        """Braces around items[lo:hi] are safe if no name declared inside
        them is mentioned at or after position `region_end`."""
        for name in self._decls(lo, hi):
            if self.last_use.get(name, -1) >= region_end:
                return False
        return True

    # -- main recursion ----------------------------------------------------
    def build(self, lo: int, hi: int, ends: Set[str], loops: List[Tuple[Optional[str], Optional[str]]]):
        """Structured nodes for items[lo:hi].  `ends` are labels that sit
        at position `hi` (immediately after this region), `loops` the
        enclosing structured loops as (break_label, continue_label)."""
        out: list = []
        i = lo
        items = self.items
        while i < hi:
            it = items[i]
            if it.kind == "label":
                res = self._try_loop(i, hi, loops, out)
                if res is not None:
                    node, nxt = res
                    out.append(node)
                    i = nxt
                    continue
                out.append(("label", it.target))
                i += 1
            elif it.kind == "goto":
                t = it.target
                if loops and loops[-1][0] is not None and t == loops[-1][0]:
                    out.append(("break",))
                elif loops and loops[-1][1] is not None and t == loops[-1][1]:
                    out.append(("continue",))
                else:
                    out.append(("goto", t))
                i += 1
            elif it.kind == "iffalse":
                res = self._try_if(i, hi, ends, loops)
                if res is not None:
                    node, nxt = res
                    out.append(node)
                    i = nxt
                else:
                    out.append(("ifgoto", _negate(it.text, it.prec), it.target))
                    i += 1
            else:
                out.append(("stmt", it.text, it.expr, it.decl))
                i += 1
        return out

    # -- if / if-else ------------------------------------------------------
    def _try_if(self, i, hi, ends, loops):
        items = self.items
        it = items[i]
        t = it.target
        cond = it.text
        neg = _negate(cond, it.prec)
        # `if (!c) goto <loop end>` / `<loop continue>` -> break / continue
        if loops:
            brk, cont = loops[-1]
            if brk is not None and t == brk:
                return ("if", neg, [("break",)], None), i + 1
            if cont is not None and t == cont:
                return ("if", neg, [("continue",)], None), i + 1

        # where does the jump land?
        if t in ends:
            e = hi
            consumed_at = None            # the label belongs to an enclosing region
        else:
            e = None
            for j in range(i + 1, hi):
                if items[j].kind == "label" and items[j].target == t:
                    e = j
                    break
            if e is None:
                return None
            consumed_at = e

        # if / else:   ...then...; goto END; ELSE: ...else...; END:
        if (consumed_at is not None and e - 1 > i and items[e - 1].kind == "goto"
                and items[e - 1].target.startswith("L_endif")):
            u_name = items[e - 1].target
            u = None
            if u_name in ends:
                u = hi
            else:
                for j in range(e + 1, hi):
                    if items[j].kind == "label" and items[j].target == u_name:
                        u = j
                        break
            if u is not None:
                if (self._scope_ok(i + 1, e - 1, e - 1)
                        and self._scope_ok(e + 1, u, u)
                        and not self._used_between(self._decls(i + 1, e - 1), e + 1, u)):
                    then = self.build(i + 1, e - 1, set(), loops)
                    els_ends = {u_name} | (ends if u == hi else set())
                    els = self.build(e + 1, u, els_ends, loops)
                    # the END label sits at `u`; the enclosing region consumes
                    # it when u == hi, otherwise it is consumed here
                    nxt = u + 1 if u != hi else hi
                    self.ifs_built += 1
                    return self._make_if(cond, neg, it.prec, then, els), nxt

        # plain if:   ...then...; END:
        if not self._scope_ok(i + 1, e, e):
            return None
        sub_ends = {t}
        if e == hi:
            sub_ends |= ends
        then = self.build(i + 1, e, sub_ends, loops)
        self.ifs_built += 1
        nxt = e + 1 if consumed_at is not None else hi
        return self._make_if(cond, neg, it.prec, then, None), nxt

    def _used_between(self, names, lo, hi) -> bool:
        for name in names:
            for k in range(lo, hi):
                if name in self.items[k].tokens():
                    return True
        return False

    @staticmethod
    def _make_if(cond, neg, prec, then, els):
        if not then and not els:
            return ("stmt", f"(void)({cond});", False, None)
        if not then:
            return ("if", neg, els, None)
        return ("if", cond, then, els if els else None)

    # -- loops -------------------------------------------------------------
    def _try_loop(self, i, hi, loops, out):
        items = self.items
        start = items[i].target
        m = _LOOP_START_RE.match(start)
        if not m:
            return None
        kind, num = m.group(1), int(m.group(2))
        if kind == "while":
            cont_name, end_name = None, f"L_endwhile{num + 1}"
        else:
            cont_name, end_name = f"L_forcont{num + 1}", f"L_endfor{num + 2}"

        # back edge: the last `goto START` inside this region
        b = None
        for j in range(hi - 1, i, -1):
            if items[j].kind == "goto" and items[j].target == start:
                b = j
                break
        if b is None:
            return None

        end_pos = self.label_at.get(end_name)
        if end_pos is not None and end_pos != b + 1:
            return None          # the loop's exit label is not where we expect it
        if end_pos is not None and end_pos >= hi:
            return None

        # loop condition: an `if (!c) goto END` right at the top
        cond_item = None
        body_lo = i + 1
        if (body_lo < b and items[body_lo].kind == "iffalse"
                and end_pos is not None and items[body_lo].target == end_name):
            cond_item = items[body_lo]
            body_lo += 1

        body_hi = b
        step_lo = step_hi = None
        if kind == "for":
            c = self.label_at.get(cont_name)
            if c is not None and body_lo <= c < b:
                body_hi, step_lo, step_hi = c, c + 1, b

        # scope: nothing declared inside the loop may be used after it
        if not self._scope_ok(i + 1, b, b + 1):
            return None

        brk = end_name if end_pos is not None else None
        cont = None
        if kind == "while":
            cont = start
        elif step_lo is not None:
            cont = cont_name

        # the step of a `for` must be plain expressions to live in the header
        step_exprs: Optional[List[str]] = None
        if step_lo is not None:
            step_items = items[step_lo:step_hi]
            if all(s.kind == "stmt" and s.expr and not s.decl for s in step_items):
                step_exprs = [s.text.rstrip(";") for s in step_items]
            elif cont is not None and self._refs(cont_name, body_lo, body_hi):
                return None      # a `continue` would skip a step we cannot place in the header

        body = self.build(body_lo, body_hi, set(), loops + [(brk, cont)])
        cond = cond_item.text if cond_item is not None else None
        nxt = b + 1 + (1 if end_pos is not None else 0)
        self.loops_built += 1

        if kind == "while":
            return ("while", cond, body), nxt

        if step_lo is not None and step_exprs is None:
            # no continue references the step: append it to the body
            body = body + self.build(step_lo, step_hi, set(), loops)
            step_exprs = []
        init = None
        step = ", ".join(step_exprs) if step_exprs else None
        header = " ".join(x for x in (cond, step) if x)
        # pull the statement just before the loop into the header, but only
        # when it initialises something the header actually uses
        if header and out and out[-1][0] == "stmt":
            _k, text, is_expr, decl = out[-1]
            if decl:
                var = decl
                ok = (not (var[0] == "t" and var[1:].isdigit())      # not a compiler temp
                      and "=" in text and "[" not in text.split("=")[0]
                      and self.last_use.get(var, -1) < nxt)           # local to the loop
            else:
                m2 = re.match(r"^([A-Za-z_]\w*) = ", text)
                var = m2.group(1) if m2 else None
                ok = bool(is_expr and var)
            if ok and var in set(_TOKEN_RE.findall(header)):
                init = text.rstrip(";")
                out.pop()
        if init is None and step is None:
            return ("while", cond, body), nxt
        return ("for", init, cond, step, body), nxt

    def _refs(self, label, lo, hi) -> bool:
        return any(it.kind in ("goto", "iffalse") and it.target == label
                   for it in self.items[lo:hi])


# --------------------------------------------------------------------------
# layout
# --------------------------------------------------------------------------
def _walk(nodes, fn):
    for nd in nodes:
        fn(nd)
        k = nd[0]
        if k == "if":
            _walk(nd[2], fn)
            if nd[3]:
                _walk(nd[3], fn)
        elif k == "while":
            _walk(nd[2], fn)
        elif k == "for":
            _walk(nd[4], fn)


def _prune_dead_labels(nodes, referenced):
    out = []
    for nd in nodes:
        k = nd[0]
        if k == "label" and nd[1] not in referenced:
            continue
        if k == "if":
            nd = ("if", nd[1], _prune_dead_labels(nd[2], referenced),
                  _prune_dead_labels(nd[3], referenced) if nd[3] else None)
        elif k == "while":
            nd = ("while", nd[1], _prune_dead_labels(nd[2], referenced))
        elif k == "for":
            nd = ("for", nd[1], nd[2], nd[3], _prune_dead_labels(nd[4], referenced))
        out.append(nd)
    return out


def _layout(nodes, depth, lines):
    pad = "    " * depth
    for nd in nodes:
        k = nd[0]
        if k == "stmt":
            lines.append(f"{pad}{nd[1]}")
        elif k == "label":
            lines.append(f"{nd[1]}:;")
        elif k == "goto":
            lines.append(f"{pad}goto {nd[1]};")
        elif k == "ifgoto":
            lines.append(f"{pad}if ({nd[1]}) goto {nd[2]};")
        elif k == "break":
            lines.append(f"{pad}break;")
        elif k == "continue":
            lines.append(f"{pad}continue;")
        elif k == "if":
            _layout_if(nd, depth, lines, "if")
        elif k == "while":
            lines.append(f"{pad}while ({nd[1] if nd[1] else '1'}) {{")
            _layout(nd[2], depth + 1, lines)
            lines.append(f"{pad}}}")
        elif k == "for":
            init, cond, step = nd[1] or "", nd[2] or "", nd[3] or ""
            head = f"{init}; {cond}; {step}".rstrip()
            lines.append(f"{pad}for ({head}) {{")
            _layout(nd[4], depth + 1, lines)
            lines.append(f"{pad}}}")


def _layout_if(nd, depth, lines, lead):
    pad = "    " * depth
    lines.append(f"{pad}{lead} ({nd[1]}) {{")
    _layout(nd[2], depth + 1, lines)
    els = nd[3]
    if els:
        if len(els) == 1 and els[0][0] == "if":
            lines.append(f"{pad}}} else")
            # glue "else" and the nested "if" onto one line
            sub: List[str] = []
            _layout_if(els[0], depth, sub, "if")
            sub[0] = sub[0][len(pad):]
            lines[-1] = f"{pad}}} else {sub[0]}"
            lines.extend(sub[1:])
            return
        lines.append(f"{pad}}} else {{")
        _layout(els, depth + 1, lines)
    lines.append(f"{pad}}}")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def structure(items: List[Item]) -> Tuple[List[str], dict]:
    """Layout lines for one function body plus a small info dict:
    {"structured": bool, "loops": int, "ifs": int}.

    Falls back to the flat label/goto form whenever the structured form
    cannot be proven to contain every jump it started with."""
    info = {"structured": False, "loops": 0, "ifs": 0}
    if not any(it.kind in ("label", "goto", "iffalse") for it in items):
        return render_flat(items), info
    try:
        s = _Structurer(items)
        nodes = s.build(0, len(items), set(), [])
    except RecursionError:
        return render_flat(items), info

    # every remaining jump must still have its label
    targets: Set[str] = set()
    labels: Set[str] = set()

    def collect(nd):
        if nd[0] == "label":
            labels.add(nd[1])
        elif nd[0] == "goto":
            targets.add(nd[1])
        elif nd[0] == "ifgoto":
            targets.add(nd[2])

    _walk(nodes, collect)
    if not targets <= labels:
        return render_flat(items), info

    nodes = _prune_dead_labels(nodes, targets)
    lines: List[str] = []
    _layout(nodes, 1, lines)
    info.update(structured=True, loops=s.loops_built, ifs=s.ifs_built)
    return lines, info


def control_flow_summary(c_text: str) -> dict:
    """Counts taken from the *emitted C text itself*, so a report can only
    mention structured loops or leftover gotos that really are in the
    output.  (Counted per line start, so a string or comment containing
    the word cannot be mistaken for a statement.)"""
    loops = ifs = gotos = 0
    for line in c_text.splitlines():
        t = line.strip()
        if re.match(r"(for|while) \(", t):
            loops += 1
        elif re.match(r"(\} else )?if \(", t):
            ifs += 1
        if re.match(r"(if \(.*\) )?goto \w+;$", t):
            gotos += 1
    return {"loops": loops, "ifs": ifs, "gotos": gotos}
