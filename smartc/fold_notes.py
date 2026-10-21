"""
Folding notes
-------------
Constant folding and dead-store removal can leave a function with no local
variables at all (`printf("%d\\n", 60);`), which hides how the numbers were
arrived at.  This module works out, from the IR *before* optimisation, which
plain integer variables have a compile-time value, so the emitter can list
them in a comment.  It never changes the program; it only describes it.
"""
import re
from typing import Dict, List, Optional, Tuple

INT_TYPES = {"int", "short", "long", "char", "unsigned", "unsigned int", "long long",
             "unsigned char", "unsigned short", "unsigned long", "signed char"}
_NUM = re.compile(r"-?\d+")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _is_temp(n) -> bool:
    return isinstance(n, str) and re.fullmatch(r"t\d+", n) is not None


def _apply(op: str, a: int, b: int) -> Optional[int]:
    try:
        if op == "+": r = a + b
        elif op == "-": r = a - b
        elif op == "*": r = a * b
        elif op == "/": r = int(a / b) if b else None
        elif op == "%": r = a - b * int(a / b) if b else None
        else: return None
    except Exception:
        return None
    if r is None or not (-2**31 <= r < 2**31):
        return None            # would overflow int: do not claim a value
    return r


def _display(name: str) -> str:
    return name.split("__")[0]


def folding_trace(instrs) -> List[Tuple[str, str, str, int]]:
    """[(variable, 'a + b' or '10', value)] for each integer variable that is
    assigned a compile-time constant in the straight-line start of a function."""
    int_vars = {i.dest for i in instrs if i.op == "DECL" and i.arg1 is None
                and str(i.extra) in INT_TYPES}
    taken = {str(i.arg1).lstrip("&") for i in instrs if i.op == "UN" and i.extra == "&"}
    env: Dict[str, int] = {}
    texts: Dict[str, str] = {}
    out: List[Tuple[str, str, str, int]] = []

    def val(x) -> Optional[int]:
        s = str(x)
        if _NUM.fullmatch(s):
            return int(s)
        return env.get(s)

    for ins in instrs:
        if ins.op in ("LABEL", "GOTO", "IF_FALSE", "RETURN"):
            break
        if ins.op == "BIN" and _is_temp(ins.dest):
            a, b = val(ins.arg1), val(ins.arg2)
            r = _apply(ins.extra, a, b) if a is not None and b is not None else None
            if r is None:
                env.pop(ins.dest, None)
            else:
                env[ins.dest] = r
                texts[ins.dest] = f"{ins.arg1} {ins.extra} {ins.arg2}"
        elif ins.op == "ASSIGN" and ins.dest in int_vars and ins.dest not in taken:
            v = val(ins.arg1)
            if v is None:
                env.pop(ins.dest, None)
                continue
            src = str(ins.arg1)
            raw = texts.get(src) if _is_temp(src) else src
            raw = raw or str(v)
            # operand values as they were just before this assignment
            filled = _IDENT.sub(lambda m: str(env.get(m.group(0), m.group(0))), raw)
            shown = _IDENT.sub(lambda m: _display(m.group(0)), raw)
            env[ins.dest] = v
            out.append((ins.dest, shown, filled, v))
        elif ins.dest and ins.op not in ("DECL", "CALL"):
            env.pop(ins.dest, None)
    return out


def describe(entry, kept: bool = False) -> str:
    name, expr, filled, v = entry
    parts = [_display(name), expr]
    if filled != expr and filled != str(v):
        parts.append(filled)
    if expr != str(v):
        parts.append(str(v))
    else:
        parts = [_display(name), str(v)]
    text = " = ".join(parts)
    return text + "   (kept below as a variable)" if kept else text


def first_printed(instrs, candidates) -> Optional[str]:
    """The first candidate variable that is passed to a call (typically
    printf) and is assigned exactly once, so keeping it costs nothing."""
    names = set(candidates)
    writes: Dict[str, int] = {}
    for ins in instrs:
        if ins.op in ("ASSIGN", "BIN", "UN", "CAST", "CALL") and isinstance(ins.dest, str):
            writes[ins.dest] = writes.get(ins.dest, 0) + 1
    for ins in instrs:
        if ins.op == "CALL":
            for a in (ins.arg1 or []):
                if str(a) in names and writes.get(str(a)) == 1:
                    return str(a)
    return None


def mentioned_names(instrs) -> set:
    names = set()
    for i in instrs:
        for a in (i.dest, i.arg1, i.arg2):
            if isinstance(a, list):
                for x in a:
                    names |= set(_IDENT.findall(str(x)))
            elif a is not None:
                names |= set(_IDENT.findall(str(a)))
    return names


def note_lines(gone, kept_entry=None) -> List[str]:
    """The text of the comment: only the operations that were carried out at
    compile time, plus one line naming the plain constants that were
    substituted (so the old declarations are not repeated line by line)."""
    ops = [t for t in gone if t[1] != str(t[3])]
    plain = [t for t in gone if t[1] == str(t[3])]
    lines = []
    if ops or kept_entry is not None:
        lines.append("Worked out at compile time:")
        lines += [f"  {describe(t)}" for t in ops]
        if kept_entry is not None:
            lines.append(f"  {describe(kept_entry, kept=True)}")
    if plain:
        lines.append("Constants substituted: " + ", ".join(describe(t) for t in plain))
    return lines
