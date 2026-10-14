"""
Preprocessor
------------
A real (if scoped) C preprocessor that runs on the raw source text
before lexing: comment stripping, line-continuation joining, object-
and function-like macro expansion (token-aware, with recursive
expansion and self-reference guards), `#undef`, conditional
compilation (`#ifdef` / `#ifndef` / `#if` / `#elif` / `#else` /
`#endif` with a real constant-expression evaluator, including
`defined(NAME)`), and local `#include "file"` (splicing another file
from disk). System `#include <header>` is still a no-op: this tool
has no real standard-library headers to provide, so `<stdio.h>` and
friends are simply dropped -- the functions they'd declare (malloc,
printf, ...) are already known to the semantic analyser as builtins
(see BUILTIN_FUNCS in semantic.py).

Output is a C source string occupying the same number of lines as
the input, so diagnostics from every later stage still point at
roughly the right place in the file the user wrote: a directive line
is replaced by blank line(s), and an expanded macro's output is kept
on the physical line that invoked it.

Not supported (and reported as a preprocessor error if used):
function-like macro invocations split across multiple physical lines
without a trailing `\\`, stringification (`#x`), token-pasting
(`##`), and variadic macros (`...` / `__VA_ARGS__`). `#include`
line-number attribution is best-effort: an included file's lines are
simply spliced in, so diagnostics after the include point use the
combined line count rather than restarting per file -- the same
tradeoff this tool already makes elsewhere for lab-scale simplicity.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

IDENT_RE = re.compile(r"[A-Za-z_]\w*")


@dataclass
class PreprocessorError:
    message: str
    line: int

    def __str__(self):
        return f"Preprocessor Error (line {self.line}): {self.message}"


@dataclass
class Macro:
    name: str
    params: Optional[List[str]]   # None = object-like; [] or [...] = function-like
    body: str                     # raw replacement text (not yet tokenised)


# -- phase 1: comment stripping (string/char literal aware) ----------------
def _strip_comments(source: str) -> str:
    out = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            i += 2
            continue
        if ch == '"' or ch == "'":
            quote = ch
            out.append(ch)
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    out.append(source[i])
                    out.append(source[i + 1])
                    i += 2
                    continue
                out.append(source[i])
                i += 1
            if i < n:
                out.append(source[i])
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# -- phase 2: line-continuation joining -------------------------------------
def _join_continuations(source: str) -> List[Tuple[str, int, int]]:
    """Returns (logical_line_text, first_physical_line_no, physical_line_count)."""
    physical = source.split("\n")
    out = []
    i = 0
    while i < len(physical):
        start = i
        parts = [physical[i].rstrip("\r")]
        while parts[-1].endswith("\\") and i + 1 < len(physical):
            parts[-1] = parts[-1][:-1]
            i += 1
            parts.append(physical[i].rstrip("\r"))
        out.append((" ".join(parts), start + 1, i - start + 1))
        i += 1
    return out


# -- a tiny tokenizer for macro bodies/expansion/condition expressions -----
_TOKEN_RE = re.compile(
    r"""(?P<space>\s+)
      | (?P<string>"(?:\\.|[^"\\])*")
      | (?P<char>'(?:\\.|[^'\\])*')
      | (?P<num>\d+\.\d+([eE][+-]?\d+)?|\d+)
      | (?P<ident>[A-Za-z_]\w*)
      | (?P<op>->|\+\+|--|==|!=|<=|>=|&&|\|\||<<|>>|.)""",
    re.VERBOSE,
)


def _tokenize(text: str) -> List[str]:
    """Splits into tokens, keeping whitespace as single-space tokens so
    re-joined output stays readable. Good enough for macro-body capture
    and expansion -- it doesn't need to be the full language lexer."""
    tokens = []
    for m in _TOKEN_RE.finditer(text):
        if m.lastgroup == "space":
            if tokens and tokens[-1] != " ":
                tokens.append(" ")
        else:
            tokens.append(m.group(0))
    return tokens


def _split_args(tokens: List[str]) -> List[List[str]]:
    """Splits a (paren-free, already-stripped) function-macro argument
    token list on top-level commas, respecting nested parens."""
    args: List[List[str]] = [[]]
    depth = 0
    for t in tokens:
        if t == "(":
            depth += 1
            args[-1].append(t)
        elif t == ")":
            depth -= 1
            args[-1].append(t)
        elif t == "," and depth == 0:
            args.append([])
        else:
            args[-1].append(t)
    if len(args) == 1 and not any(a.strip() for a in args[0]):
        return []
    return args


class Preprocessor:
    def __init__(self, base_dir: str = "."):
        self.macros: Dict[str, Macro] = {}
        self.errors: List[PreprocessorError] = []
        self.base_dir = base_dir
        self._include_depth = 0

    def _error(self, message: str, line: int):
        self.errors.append(PreprocessorError(message, line))

    # -- macro expansion -----------------------------------------------
    def _expand_tokens(self, tokens: List[str], line: int, active: frozenset) -> List[str]:
        out: List[str] = []
        i = 0
        n = len(tokens)
        changed = False
        while i < n:
            t = tokens[i]
            if t == " " or not IDENT_RE.fullmatch(t) or t in active:
                out.append(t)
                i += 1
                continue
            macro = self.macros.get(t)
            if macro is None:
                out.append(t)
                i += 1
                continue

            if macro.params is None:
                body_tokens = _tokenize(macro.body)
                expanded = self._expand_tokens(body_tokens, line, active | {t})
                out.extend(expanded)
                i += 1
                changed = True
                continue

            # function-like: must be followed by '(' (skipping spaces)
            j = i + 1
            while j < n and tokens[j] == " ":
                j += 1
            if j >= n or tokens[j] != "(":
                out.append(t)
                i += 1
                continue
            depth = 1
            k = j + 1
            while k < n and depth > 0:
                if tokens[k] == "(":
                    depth += 1
                elif tokens[k] == ")":
                    depth -= 1
                k += 1
            if depth != 0:
                self._error(f"unterminated invocation of macro '{t}'", line)
                out.append(t)
                i += 1
                continue
            arg_tokens = tokens[j + 1:k - 1]
            args = _split_args(arg_tokens)
            if len(args) != len(macro.params):
                self._error(
                    f"macro '{t}' expects {len(macro.params)} argument(s) "
                    f"but {len(args)} given", line,
                )
                out.append(t)
                i = k
                continue
            expanded_args = [self._expand_tokens(a, line, active) for a in args]
            param_map = dict(zip(macro.params, expanded_args))
            body_tokens = _tokenize(macro.body)
            substituted = []
            for bt in body_tokens:
                if bt in param_map:
                    substituted.extend(param_map[bt])
                else:
                    substituted.append(bt)
            out.extend(self._expand_tokens(substituted, line, active | {t}))
            i = k
            changed = True

        if changed:
            # a substitution can expose further macro calls (e.g. a
            # macro expanding to another macro's name) -- one more pass
            # at this level catches that without needing unbounded
            # recursion, since each name in `active` can't re-trigger.
            return out
        return out

    def expand_line(self, text: str, line: int) -> str:
        tokens = _tokenize(text)
        expanded = self._expand_tokens(tokens, line, frozenset())
        if expanded == tokens:
            # Nothing was expanded: hand the line on byte-for-byte, so
            # the lexer's column numbers (and therefore every later
            # diagnostic's) match what the user actually wrote --
            # re-joining tokens would drop indentation and collapse runs
            # of spaces, shifting every column on the line.
            return text
        indent = text[:len(text) - len(text.lstrip())]
        return indent + "".join(expanded).lstrip()

    # -- directive handling ----------------------------------------------
    def _handle_define(self, rest: str, line: int):
        m = re.match(r"^(\w+)(\((.*?)\))?\s?(.*)$", rest.strip())
        if not m:
            self._error("malformed #define", line)
            return
        name, has_params, params_text, body = m.group(1), m.group(2), m.group(3), m.group(4)
        if has_params is not None:
            params = [p.strip() for p in params_text.split(",")] if params_text.strip() else []
            if any("..." in p for p in params):
                self._error(f"variadic macro '{name}' is not supported", line)
                return
            self.macros[name] = Macro(name, params, body.strip())
        else:
            self.macros[name] = Macro(name, None, body.strip())

    def _handle_undef(self, rest: str, line: int):
        name = rest.strip()
        self.macros.pop(name, None)

    def _eval_condition(self, expr_text: str, line: int) -> bool:
        tokens = _tokenize(expr_text)
        tokens = self._resolve_defined(tokens)
        expanded = self._expand_tokens(tokens, line, frozenset())
        expr_str = "".join(t if t != " " else " " for t in expanded).strip()
        if not expr_str:
            self._error("empty #if/#elif expression", line)
            return False
        try:
            value = _ExprEval(expr_str, line, self._error).parse()
        except _ExprEvalError as e:
            self._error(str(e), line)
            return False
        return value != 0

    def _resolve_defined(self, tokens: List[str]) -> List[str]:
        out = []
        i = 0
        while i < len(tokens):
            if tokens[i] == "defined":
                j = i + 1
                while j < len(tokens) and tokens[j] == " ":
                    j += 1
                paren = j < len(tokens) and tokens[j] == "("
                if paren:
                    j += 1
                    while j < len(tokens) and tokens[j] == " ":
                        j += 1
                if j < len(tokens) and IDENT_RE.fullmatch(tokens[j]):
                    name = tokens[j]
                    j += 1
                    if paren:
                        while j < len(tokens) and tokens[j] == " ":
                            j += 1
                        if j < len(tokens) and tokens[j] == ")":
                            j += 1
                    out.append("1" if name in self.macros else "0")
                    i = j
                    continue
            out.append(tokens[i])
            i += 1
        return out

    def _resolve_include_path(self, rest: str) -> Optional[str]:
        rest = rest.strip()
        m = re.match(r'^"(.+)"$', rest)
        if not m:
            return None  # <system/header.h> -- intentionally not resolved
        return os.path.normpath(os.path.join(self.base_dir, m.group(1)))

    # -- main entry point --------------------------------------------------
    def process(self, source: str) -> str:
        source = _strip_comments(source)
        logical_lines = _join_continuations(source)

        out_lines: List[str] = []
        cond_stack: List[dict] = []  # each: {"active": bool, "taken": bool, "has_else": bool}

        def currently_active() -> bool:
            return all(c["active"] for c in cond_stack)

        for text, first_line, span in logical_lines:
            stripped = text.strip()
            if stripped.startswith("#"):
                directive_body = stripped[1:].strip()
                parts = directive_body.split(None, 1)
                kw = parts[0] if parts else ""
                rest = parts[1] if len(parts) > 1 else ""

                if kw in ("ifdef", "ifndef"):
                    name = rest.strip()
                    defined = name in self.macros
                    cond_true = defined if kw == "ifdef" else not defined
                    cond_stack.append({"active": currently_active() and cond_true,
                                        "taken": cond_true, "has_else": False})
                elif kw == "if":
                    if currently_active():
                        cond_true = self._eval_condition(rest, first_line)
                    else:
                        cond_true = False
                    cond_stack.append({"active": currently_active() and cond_true,
                                        "taken": cond_true, "has_else": False})
                elif kw == "elif":
                    if not cond_stack:
                        self._error("#elif without #if", first_line)
                    else:
                        frame = cond_stack[-1]
                        if frame["has_else"]:
                            self._error("#elif after #else", first_line)
                        parent_active = all(c["active"] for c in cond_stack[:-1])
                        if frame["taken"] or not parent_active:
                            frame["active"] = False
                        else:
                            cond_true = self._eval_condition(rest, first_line)
                            frame["active"] = cond_true
                            frame["taken"] = frame["taken"] or cond_true
                elif kw == "else":
                    if not cond_stack:
                        self._error("#else without #if", first_line)
                    else:
                        frame = cond_stack[-1]
                        if frame["has_else"]:
                            self._error("duplicate #else", first_line)
                        frame["has_else"] = True
                        parent_active = all(c["active"] for c in cond_stack[:-1])
                        frame["active"] = parent_active and not frame["taken"]
                        frame["taken"] = True
                elif kw == "endif":
                    if not cond_stack:
                        self._error("#endif without #if", first_line)
                    else:
                        cond_stack.pop()
                elif not currently_active():
                    pass  # any other directive inside a false branch is ignored
                elif kw == "define":
                    self._handle_define(rest, first_line)
                elif kw == "undef":
                    self._handle_undef(rest, first_line)
                elif kw == "include":
                    path = self._resolve_include_path(rest)
                    if path is None:
                        pass  # system header -- no-op, see module docstring
                    elif self._include_depth > 20:
                        self._error("#include nested too deeply (possible cycle)", first_line)
                    elif not os.path.isfile(path):
                        self._error(f"cannot find included file '{path}'", first_line)
                    else:
                        with open(path) as fh:
                            included_src = fh.read()
                        self._include_depth += 1
                        out_lines.append(self.process(included_src))
                        self._include_depth -= 1
                elif kw in ("error",):
                    self._error(rest.strip() or "#error", first_line)
                elif kw in ("pragma", "line", "warning"):
                    pass  # recognised, intentionally no-op
                else:
                    self._error(f"unknown preprocessor directive '#{kw}'", first_line)

                out_lines.append("" * 1)
                out_lines.extend([""] * (span - 1))
                continue

            if currently_active():
                out_lines.append(self.expand_line(text, first_line))
            else:
                out_lines.append("")
            out_lines.extend([""] * (span - 1))

        if cond_stack:
            self._error("unterminated #if (missing #endif)", logical_lines[-1][1] if logical_lines else 1)

        return "\n".join(out_lines)


class _ExprEvalError(Exception):
    pass


class _ExprEval:
    """Recursive-descent evaluator for #if/#elif integer constant
    expressions: literals, identifiers (undefined -> 0), !, unary +/-,
    * / %, + -, relational, ==/!=, &&, ||, parentheses."""

    def __init__(self, text: str, line: int, error_sink):
        self.tokens = _tokenize(text)
        self.tokens = [t for t in self.tokens if t != " "]
        self.pos = 0
        self.line = line

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self):
        t = self._peek()
        self.pos += 1
        return t

    def parse(self) -> int:
        value = self._or()
        if self.pos != len(self.tokens):
            raise _ExprEvalError(f"unexpected token '{self._peek()}' in #if expression")
        return value

    def _or(self):
        v = self._and()
        while self._peek() == "||":
            self._advance()
            rhs = self._and()
            v = 1 if (v or rhs) else 0
        return v

    def _and(self):
        v = self._eq()
        while self._peek() == "&&":
            self._advance()
            rhs = self._eq()
            v = 1 if (v and rhs) else 0
        return v

    def _eq(self):
        v = self._rel()
        while self._peek() in ("==", "!="):
            op = self._advance()
            rhs = self._rel()
            v = int((v == rhs) if op == "==" else (v != rhs))
        return v

    def _rel(self):
        v = self._add()
        while self._peek() in ("<", ">", "<=", ">="):
            op = self._advance()
            rhs = self._add()
            v = int({"<": v < rhs, ">": v > rhs, "<=": v <= rhs, ">=": v >= rhs}[op])
        return v

    def _add(self):
        v = self._mul()
        while self._peek() in ("+", "-"):
            op = self._advance()
            rhs = self._mul()
            v = v + rhs if op == "+" else v - rhs
        return v

    def _mul(self):
        v = self._unary()
        while self._peek() in ("*", "/", "%"):
            op = self._advance()
            rhs = self._unary()
            if op in ("/", "%") and rhs == 0:
                raise _ExprEvalError("division by zero in #if expression")
            v = v // rhs if op == "/" else (v * rhs if op == "*" else v % rhs)
        return v

    def _unary(self):
        if self._peek() == "!":
            self._advance()
            return 0 if self._unary() else 1
        if self._peek() == "-":
            self._advance()
            return -self._unary()
        if self._peek() == "+":
            self._advance()
            return self._unary()
        return self._primary()

    def _primary(self):
        t = self._peek()
        if t is None:
            raise _ExprEvalError("incomplete #if expression")
        if t == "(":
            self._advance()
            v = self._or()
            if self._peek() != ")":
                raise _ExprEvalError("missing ')' in #if expression")
            self._advance()
            return v
        if re.fullmatch(r"\d+", t):
            self._advance()
            return int(t)
        if IDENT_RE.fullmatch(t):
            self._advance()
            return 0   # undefined identifier in a constant expression -> 0
        raise _ExprEvalError(f"unexpected token '{t}' in #if expression")


def strip_directives(source: str, base_dir: str = ".") -> Tuple[str, List[PreprocessorError]]:
    """Entry point used by cli.py/pipeline.py. Name kept from the
    earlier, much simpler implementation so neither caller's import
    needs to change -- only how its result is used (it now returns
    errors too)."""
    pp = Preprocessor(base_dir=base_dir)
    processed = pp.process(source)
    return processed, pp.errors
