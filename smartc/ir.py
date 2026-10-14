"""
Intermediate Code Generation
-----------------------------
Lowers the AST into a simple three-address code (TAC): a flat list of
instructions, each with at most one operator and a handful of
operands, using temporaries (t0, t1, ...) to hold intermediate
results. This is the representation the optimizer passes operate on,
mirroring the "front end builds an IR, middle end optimizes it" shape
of a real compiler.

Instruction forms produced here:
    ASSIGN    dest = arg1
    BIN       dest = arg1 <op> arg2
    UN        dest = <op> arg1
    DECL      declare dest (type in `extra`; array dims in `arg1`). For an
              array declared with an initialiser, `arg2` holds it: a
              (nested) list of operands for `{...}`, or a string literal
              for `char s[] = "..."` -- C semantics (unlisted elements
              are zero) are kept by emitting it back as an initialiser.
    LABEL     L:
    GOTO      goto L
    IF_FALSE  if not arg1 goto L
    CALL      dest = call extra(arg1=[...])   (dest may be None for void calls)
    RETURN    return arg1   (arg1 may be None)
    CAST      dest = (extra) arg1             (extra is the target type, e.g. "int*")

Pointers piggyback on the existing operand forms rather than needing
their own instruction kind: `&x` / `*p` lower to a plain UN
instruction (extra="&"/"*"), and a dereference used as an assignment
target (`*p = v`) becomes an ASSIGN/BIN whose `dest` is the literal
string "*p" -- the same trick `arg[i]` already uses for element
stores, so one instruction form covers both.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Any

from .ast_nodes import (
    Program, FuncDecl, VarDecl, DeclGroup, Block, If, While, For, Return, Break,
    Continue, ExprStmt, Assign, BinOp, UnaryOp, Literal, Identifier,
    ArrayAccess, Call, Cast, Sizeof, StructDecl, MemberAccess, InitList,
)

COMPOUND_TO_BINOP = {
    "+=": "+", "-=": "-", "*=": "*", "/=": "/", "%=": "%",
}


@dataclass
class Instr:
    op: str
    dest: Optional[str] = None
    arg1: Optional[Any] = None
    arg2: Optional[Any] = None
    extra: Optional[Any] = None   # operator symbol, function name, or type
    line: int = 0

    def __str__(self):
        if self.op == "LABEL":
            return f"{self.dest}:"
        if self.op == "GOTO":
            return f"    goto {self.dest}"
        if self.op == "IF_FALSE":
            return f"    if_false {self.arg1} goto {self.dest}"
        if self.op == "DECL":
            if isinstance(self.arg1, list):
                size = "".join(f"[{d}]" for d in self.arg1)
            elif self.arg1 is not None:
                size = f"[{self.arg1}]"
            else:
                size = ""
            init = f" = {_format_init(self.arg2)}" if self.arg2 is not None else ""
            return f"    decl {self.extra} {self.dest}{size}{init}"
        if self.op == "CAST":
            return f"    {self.dest} = ({self.extra}){self.arg1}"
        if self.op == "ASSIGN":
            return f"    {self.dest} = {self.arg1}"
        if self.op == "BIN":
            return f"    {self.dest} = {self.arg1} {self.extra} {self.arg2}"
        if self.op == "UN":
            return f"    {self.dest} = {self.extra}{self.arg1}"
        if self.op == "CALL":
            args = ", ".join(str(a) for a in (self.arg1 or []))
            if self.dest:
                return f"    {self.dest} = call {self.extra}({args})"
            return f"    call {self.extra}({args})"
        if self.op == "RETURN":
            return f"    return {self.arg1}" if self.arg1 is not None else "    return"
        return f"    {self.op} {self.dest} {self.arg1} {self.arg2} {self.extra}"


def _format_init(init) -> str:
    if isinstance(init, list):
        return "{" + ", ".join(_format_init(x) for x in init) + "}" if init else "{0}"
    return str(init)


def nest_initializer(values, dims) -> list:
    """(flat_index, operand) pairs -> nested lists matching `dims`,
    filling gaps with "0" and stopping after the last listed element
    (C zero-fills the rest, so trailing zeros needn't be spelled out)."""
    if not values:
        return []
    last = max(i for i, _ in values)
    flat = ["0"] * (last + 1)
    for i, v in values:
        flat[i] = v

    def nest(seq, level):
        if level >= len(dims) - 1:
            return list(seq)
        row = 1
        for d in dims[level + 1:]:
            row *= d
        return [nest(seq[k:k + row], level + 1) for k in range(0, len(seq), row)]

    return nest(flat, 0)


_SAFE_BINOPS = {"+", "-", "*", "<", ">", "<=", ">=", "==", "!=", "&&", "||"}


def _is_pure_and_safe(node) -> bool:
    """True if evaluating `node` can neither change anything nor fault
    (no call, assignment, ++/--, division, dereference or array/member
    access). Only then may the right operand of `&&` / `||` be computed
    eagerly, which keeps the common `a < b && b < c` as straight-line code."""
    if isinstance(node, (Literal, Identifier, Sizeof)):
        return True
    if isinstance(node, BinOp):
        return node.op in _SAFE_BINOPS and _is_pure_and_safe(node.left) \
            and _is_pure_and_safe(node.right)
    if isinstance(node, UnaryOp):
        return node.op in ("-", "!") and not node.postfix and _is_pure_and_safe(node.operand)
    if isinstance(node, Cast):
        return _is_pure_and_safe(node.operand)
    return False


@dataclass
class IRFunction:
    name: str
    return_type: str
    params: List[str] = field(default_factory=list)
    instructions: List[Instr] = field(default_factory=list)
    has_body: bool = True
    param_types: dict = field(default_factory=dict)   # IR param name -> type


class IRGenerator:
    def __init__(self):
        self.temp_count = 0
        self.label_count = 0
        self.instructions: List[Instr] = []
        self.loop_stack = []   # list of (continue_label, break_label)
        # Scope-aware name resolution: the AST allows an inner block to
        # shadow an outer variable (e.g. `int x = 1; { int x = 2; }`),
        # which is legal C, but this IR is a flat list with no block
        # structure of its own. Without renaming, both `x`s would share
        # one IR slot and the inner assignment would corrupt the outer
        # variable's value. So each declaration gets a unique IR name
        # (`x`, then `x__2`, `x__3`, ...) and `scopes` mirrors the AST's
        # block nesting to resolve every read/write to the right one.
        self.scopes: List[dict] = [{}]
        self.name_count: dict = {}

    def _new_temp(self) -> str:
        name = f"t{self.temp_count}"
        self.temp_count += 1
        return name

    def _new_label(self, hint: str) -> str:
        name = f"L_{hint}{self.label_count}"
        self.label_count += 1
        return name

    def _emit(self, instr: Instr):
        self.instructions.append(instr)

    # -- scope-aware naming -------------------------------------------------
    def _push_scope(self):
        self.scopes.append({})

    def _pop_scope(self):
        self.scopes.pop()

    def _declare_var(self, name: str) -> str:
        count = self.name_count.get(name, 0)
        if count == 0 and re.fullmatch(r"t\d+", name):
            # a user variable called t0, t1, ... would be mistaken for a
            # compiler temporary by every later phase
            count = 1
        ir_name = name if count == 0 else f"{name}__{count + 1}"
        self.name_count[name] = count + 1
        self.scopes[-1][name] = ir_name
        return ir_name

    def _resolve(self, name: str) -> str:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return name  # should not happen once semantic analysis has passed

    # -- entry point -------------------------------------------------------
    def generate(self, program: Program):
        functions: List[IRFunction] = []
        global_instrs: List[Instr] = []

        self.scopes = [{}]
        self.name_count = {}
        # Global variable names: a local (or parameter) that reuses one
        # must get a distinct IR name, or code later in the function that
        # means the *global* would silently read the local instead once
        # everything is flattened into one instruction list.
        self.global_names = set()
        for decl in program.declarations:
            if isinstance(decl, VarDecl):
                self.global_names.add(decl.name)
            elif isinstance(decl, DeclGroup):
                self.global_names.update(d.name for d in decl.decls)
        for decl in program.declarations:
            if isinstance(decl, FuncDecl):
                functions.append(self._generate_function(decl))
            elif isinstance(decl, StructDecl):
                pass   # a tag declaration; no runtime code, nothing to lower
            else:
                self.instructions = []
                self.temp_count = 0
                self._gen_stmt(decl)
                global_instrs.extend(self.instructions)

        return global_instrs, functions

    def _generate_function(self, node: FuncDecl) -> IRFunction:
        self.instructions = []
        self.temp_count = 0
        self.label_count = 0
        self.scopes = [{}]
        self.name_count = {g: 1 for g in getattr(self, "global_names", ())}
        param_names = [self._declare_var(p.name) for p in node.params]
        if node.body is not None:
            for stmt in node.body.statements:
                self._gen_stmt(stmt)
        return IRFunction(
            name=node.name,
            return_type=node.return_type,
            params=[p.type + " " + ir for p, ir in zip(node.params, param_names)],
            param_types={ir: p.type for p, ir in zip(node.params, param_names)},
            instructions=self.instructions,
            has_body=node.body is not None,
        )

    # -- statements ----------------------------------------------------
    def _gen_stmt(self, node):
        if node is None:
            return

        if isinstance(node, VarDecl):
            dims = node.array_dims if node.array_dims else node.array_size
            if isinstance(node.init, InitList):
                # elements are evaluated in source order (C leaves the
                # order unspecified, so any order is a correct one)
                values = [(idx, self._gen_expr(e)) for idx, e in (node.init.layout or [])]
                ir_name = self._declare_var(node.name)
                all_dims = node.array_dims if node.array_dims else [node.array_size]
                self._emit(Instr("DECL", dest=ir_name, arg1=dims, extra=node.type,
                                 arg2=nest_initializer(values, all_dims), line=node.line))
                return
            if node.array_size is not None and isinstance(node.init, Literal) \
                    and node.init.type == "string":
                ir_name = self._declare_var(node.name)
                self._emit(Instr("DECL", dest=ir_name, arg1=dims, extra=node.type,
                                 arg2=f'"{node.init.value}"', line=node.line))
                return
            ir_name = self._declare_var(node.name)
            self._emit(Instr("DECL", dest=ir_name, arg1=dims, extra=node.type, line=node.line))
            if node.init is not None:
                value = self._gen_expr(node.init)
                self._emit(Instr("ASSIGN", dest=ir_name, arg1=value, line=node.line))
            return

        if isinstance(node, DeclGroup):
            for d in node.decls:
                self._gen_stmt(d)
            return

        if isinstance(node, Block):
            self._push_scope()
            for s in node.statements:
                self._gen_stmt(s)
            self._pop_scope()
            return

        if isinstance(node, ExprStmt):
            self._gen_expr(node.expr)
            return

        if isinstance(node, If):
            cond = self._gen_expr(node.cond)
            else_label = self._new_label("else")
            end_label = self._new_label("endif")
            self._emit(Instr("IF_FALSE", dest=else_label, arg1=cond, line=node.line))
            self._gen_stmt(node.then_branch)
            if node.else_branch is not None:
                self._emit(Instr("GOTO", dest=end_label))
                self._emit(Instr("LABEL", dest=else_label))
                self._gen_stmt(node.else_branch)
                self._emit(Instr("LABEL", dest=end_label))
            else:
                self._emit(Instr("LABEL", dest=else_label))
            return

        if isinstance(node, While):
            start_label = self._new_label("while")
            end_label = self._new_label("endwhile")
            self._emit(Instr("LABEL", dest=start_label))
            cond = self._gen_expr(node.cond)
            self._emit(Instr("IF_FALSE", dest=end_label, arg1=cond, line=node.line))
            self.loop_stack.append((start_label, end_label))
            self._gen_stmt(node.body)
            self.loop_stack.pop()
            self._emit(Instr("GOTO", dest=start_label))
            self._emit(Instr("LABEL", dest=end_label))
            return

        if isinstance(node, For):
            self._push_scope()
            self._gen_stmt(node.init)
            start_label = self._new_label("for")
            continue_label = self._new_label("forcont")
            end_label = self._new_label("endfor")
            self._emit(Instr("LABEL", dest=start_label))
            if node.cond is not None:
                cond = self._gen_expr(node.cond)
                self._emit(Instr("IF_FALSE", dest=end_label, arg1=cond, line=node.line))
            self.loop_stack.append((continue_label, end_label))
            self._gen_stmt(node.body)
            self.loop_stack.pop()
            self._emit(Instr("LABEL", dest=continue_label))
            if node.update is not None:
                self._gen_expr(node.update)
            self._emit(Instr("GOTO", dest=start_label))
            self._emit(Instr("LABEL", dest=end_label))
            self._pop_scope()
            return

        if isinstance(node, Return):
            value = self._gen_expr(node.value) if node.value is not None else None
            self._emit(Instr("RETURN", arg1=value, line=node.line))
            return

        if isinstance(node, Break):
            if self.loop_stack:
                self._emit(Instr("GOTO", dest=self.loop_stack[-1][1]))
            return

        if isinstance(node, Continue):
            if self.loop_stack:
                self._emit(Instr("GOTO", dest=self.loop_stack[-1][0]))
            return

        if isinstance(node, StructDecl):
            return   # a local tag declaration; no runtime code

        raise TypeError(f"IR generator: unhandled statement {type(node)}")

    # -- expressions: returns the operand string holding the result ----
    def _gen_expr(self, node) -> Optional[str]:
        if node is None:
            return None

        if isinstance(node, Literal):
            if node.type == "string":
                return f'"{node.value}"'
            if node.type == "char":
                return f"'{node.value}'"
            return str(node.value)

        if isinstance(node, Identifier):
            return self._resolve(node.name)

        if isinstance(node, ArrayAccess):
            base = self._gen_expr(node.base)
            indices = [self._gen_expr(i) for i in node.indices]
            suffix = "".join(f"[{i}]" for i in indices)
            return f"{base}{suffix}"

        if isinstance(node, Call):
            args = [self._gen_expr(a) for a in node.args]
            temp = self._new_temp()
            self._emit(Instr("CALL", dest=temp, arg1=args, extra=node.name, line=node.line))
            return temp

        if isinstance(node, BinOp) and node.op in ("&&", "||") \
                and not _is_pure_and_safe(node.right):
            return self._gen_short_circuit(node)

        if isinstance(node, BinOp):
            left = self._gen_expr(node.left)
            right = self._gen_expr(node.right)
            temp = self._new_temp()
            self._emit(Instr("BIN", dest=temp, arg1=left, arg2=right, extra=node.op, line=node.line))
            return temp

        if isinstance(node, UnaryOp):
            return self._gen_unary(node)

        if isinstance(node, Assign):
            return self._gen_assign(node)

        if isinstance(node, Cast):
            operand = self._gen_expr(node.operand)
            temp = self._new_temp()
            self._emit(Instr("CAST", dest=temp, arg1=operand, extra=node.type, line=node.line))
            return temp

        if isinstance(node, Sizeof):
            if node.type is not None:
                return f"sizeof({node.type})"
            inner = self._gen_expr(node.expr)
            return f"sizeof({inner})"

        if isinstance(node, MemberAccess):
            base = self._gen_expr(node.base)
            return f"{base}{'->' if node.arrow else '.'}{node.member}"

        raise TypeError(f"IR generator: unhandled expression {type(node)}")

    def _gen_short_circuit(self, node: BinOp) -> str:
        """`a && b` / `a || b` must not evaluate `b` when `a` already
        decides the answer (`p != NULL && *p > 0`, `i < n && a[i]`, or a
        right operand with side effects). Lowered to branches:

            a && b:   t = 0; if !a goto end; <b>; if !b goto end; t = 1; end:
            a || b:   t = 1; na = !a; if !na goto end; <b>; nb = !b; if !nb goto end; t = 0; end:
        """
        left = self._gen_expr(node.left)
        result = self._new_temp()
        end = self._new_label("sc")
        if node.op == "&&":
            self._emit(Instr("ASSIGN", dest=result, arg1="0", line=node.line))
            self._emit(Instr("IF_FALSE", dest=end, arg1=left, line=node.line))
            right = self._gen_expr(node.right)
            self._emit(Instr("IF_FALSE", dest=end, arg1=right, line=node.line))
            self._emit(Instr("ASSIGN", dest=result, arg1="1", line=node.line))
        else:
            self._emit(Instr("ASSIGN", dest=result, arg1="1", line=node.line))
            not_left = self._new_temp()
            self._emit(Instr("UN", dest=not_left, arg1=left, extra="!", line=node.line))
            self._emit(Instr("IF_FALSE", dest=end, arg1=not_left, line=node.line))
            right = self._gen_expr(node.right)
            not_right = self._new_temp()
            self._emit(Instr("UN", dest=not_right, arg1=right, extra="!", line=node.line))
            self._emit(Instr("IF_FALSE", dest=end, arg1=not_right, line=node.line))
            self._emit(Instr("ASSIGN", dest=result, arg1="0", line=node.line))
        self._emit(Instr("LABEL", dest=end))
        return result

    def _gen_unary(self, node: UnaryOp) -> str:
        target = self._gen_expr(node.operand)

        if node.op in ("++", "--"):
            delta = "1"
            binop = "+" if node.op == "++" else "-"
            if node.postfix:
                old = self._new_temp()
                self._emit(Instr("ASSIGN", dest=old, arg1=target, line=node.line))
                self._emit(Instr("BIN", dest=target, arg1=target, arg2=delta, extra=binop, line=node.line))
                return old
            else:
                self._emit(Instr("BIN", dest=target, arg1=target, arg2=delta, extra=binop, line=node.line))
                return target

        temp = self._new_temp()
        self._emit(Instr("UN", dest=temp, arg1=target, extra=node.op, line=node.line))
        return temp

    def _gen_assign(self, node: Assign) -> str:
        target_name = self._target_name(node.target)
        value = self._gen_expr(node.value)

        if node.op == "=":
            self._emit(Instr("ASSIGN", dest=target_name, arg1=value, line=node.line))
        else:
            binop = COMPOUND_TO_BINOP[node.op]
            self._emit(Instr("BIN", dest=target_name, arg1=target_name, arg2=value, extra=binop, line=node.line))
        return target_name

    def _target_name(self, node) -> str:
        if isinstance(node, Identifier):
            return self._resolve(node.name)
        if isinstance(node, ArrayAccess):
            base = self._gen_expr(node.base)
            indices = [self._gen_expr(i) for i in node.indices]
            suffix = "".join(f"[{i}]" for i in indices)
            return f"{base}{suffix}"
        if isinstance(node, UnaryOp) and node.op == "*":
            operand = self._gen_expr(node.operand)
            return f"*{operand}"
        if isinstance(node, MemberAccess):
            return self._gen_expr(node)
        raise TypeError(f"IR generator: invalid assignment target {type(node)}")
