"""
Semantic Analyser + Symbol Table
--------------------------------
Walks the AST produced by the parser and checks it against the static
semantics of the language subset: every name must be declared before
use, names must not be redeclared in the same scope, operations must
be applied to compatible types, functions must be called with the
right number of arguments, and so on.

The symbol table is implemented as a stack of scopes (global scope at
the bottom, one scope per block pushed on entry and popped on exit).
Each entry records the identifier's kind (var/param/func), type,
declaring line, and whether it has been used -- which is what lets
this analyser flag unused variables, a nice lead-in to the dead-code
elimination pass that follows in the optimizer.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

from .ast_nodes import (
    Program, FuncDecl, VarDecl, DeclGroup, Block, If, While, For, Return, Break,
    Continue, ExprStmt, Assign, BinOp, UnaryOp, Literal, Identifier,
    ArrayAccess, Call, Param, Cast, Sizeof, StructDecl, MemberAccess, InitList,
)

NUMERIC_TYPES = {"int", "float", "char", "double"}


def string_literal_length(raw: str) -> int:
    """Number of chars a C string literal's body denotes (escape
    sequences count once; the implicit terminating NUL is not counted).
    `raw` is the text between the quotes, as the lexer stores it."""
    n, i = 0, 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "x":
                i += 2
                while i < len(raw) and raw[i] in "0123456789abcdefABCDEF":
                    i += 1
            elif nxt in "01234567":
                i += 1
                k = 0
                while i < len(raw) and raw[i] in "01234567" and k < 3:
                    i += 1
                    k += 1
            else:
                i += 2
        else:
            i += 1
        n += 1
    return n


def is_constant_expression(node) -> bool:
    """True for an expression C accepts as the initialiser of a
    file-scope (static-storage) object: built only from literals,
    sizeof, casts and operators, or the address of a global/string.
    (Identifiers, calls, assignments and dereferences are not.)"""
    if isinstance(node, Literal):
        return True
    if isinstance(node, Sizeof):
        return True
    if isinstance(node, Cast):
        return is_constant_expression(node.operand)
    if isinstance(node, UnaryOp):
        if node.op in ("-", "!"):
            return is_constant_expression(node.operand)
        if node.op == "&" and isinstance(node.operand, Identifier):
            return True   # address of an object with static storage
        return False
    if isinstance(node, BinOp):
        return is_constant_expression(node.left) and is_constant_expression(node.right)
    return False

# Library functions this tool knows the signature of without a
# preprocessor/#include -- common enough (malloc-family, stdio) that
# rejecting them as "undeclared" would make the tool useless on any
# real pointer-heavy snippet. `None` for params means "variadic / not
# checked" (printf & co).
BUILTIN_FUNCS = {
    "malloc": ("void*", ["int"]),
    "calloc": ("void*", ["int", "int"]),
    "realloc": ("void*", ["void*", "int"]),
    "free": ("void", ["void*"]),
    "printf": ("int", None),
    "scanf": ("int", None),
    "fprintf": ("int", None),
    "sprintf": ("int", None),
    "puts": ("int", ["char*"]),
    "putchar": ("int", ["int"]),
    "getchar": ("int", []),
    "gets": ("char*", ["char*"]),
    "strlen": ("int", ["char*"]),
}


def is_pointer_type(t) -> bool:
    return isinstance(t, str) and t.endswith("*")


def pointer_base(t: str) -> str:
    return t[:-1]


def is_struct_type(t) -> bool:
    """True for "struct Point", "struct Point*", "union U*", etc. --
    any type string whose non-pointer base starts with 'struct '/'union '."""
    if not isinstance(t, str):
        return False
    base = t.rstrip("*")
    return base.startswith("struct ") or base.startswith("union ")


def struct_tag(t: str) -> str:
    """"struct Point*" -> "Point"; "union U" -> "U"."""
    return t.rstrip("*").split(" ", 1)[1]


@dataclass
class Diagnostic:
    severity: str   # "error" | "warning"
    message: str
    line: int

    def __str__(self):
        label = "Semantic Error" if self.severity == "error" else "Warning"
        return f"{label} (line {self.line}): {self.message}"


@dataclass
class SymbolEntry:
    name: str
    kind: str        # "var", "param", "func", "array"
    type: str
    line: int
    scope_level: int
    is_array: bool = False
    array_size: Optional[int] = None
    array_dims: Optional[List[int]] = None   # set when there's more than one dimension
    params: Optional[List[str]] = None   # for functions: param types
    used: bool = False
    initialized: bool = True


@dataclass
class StructInfo:
    tag: str
    is_union: bool
    line: int
    fields: Dict[str, str] = field(default_factory=dict)   # name -> type
    field_order: List[str] = field(default_factory=list)
    field_array_dims: Dict[str, Optional[List[int]]] = field(default_factory=dict)


class SemanticAnalyzer:
    def __init__(self):
        self.diagnostics: List[Diagnostic] = []
        # scopes[0] is global; scopes[-1] is the innermost active scope
        self.scopes: List[Dict[str, SymbolEntry]] = [{}]
        self.current_function_return_type: Optional[str] = None
        # every declared symbol, kept for the final symbol-table report
        self.all_symbols: List[SymbolEntry] = []
        # struct/union tags are their own namespace (not variables), so
        # a struct and a variable can share a name without colliding.
        self.structs: Dict[str, StructInfo] = {}
        # library functions (malloc, printf, ...) -- looked up as a
        # fallback so they never collide with a redeclaration check and
        # never clutter the printed symbol table.
        self.builtins: Dict[str, SymbolEntry] = {
            name: SymbolEntry(name=name, kind="func", type=ret, line=0,
                               scope_level=0, params=params)
            for name, (ret, params) in BUILTIN_FUNCS.items()
        }

    # -- scope management -------------------------------------------------
    def _push_scope(self):
        self.scopes.append({})

    def _pop_scope(self):
        self.scopes.pop()

    def _declare(self, name, kind, type_, line, is_array=False,
                 array_size=None, array_dims=None, params=None):
        scope = self.scopes[-1]
        if name in scope:
            self._error(
                f"redeclaration of '{name}' (previously declared at line "
                f"{scope[name].line})",
                line,
            )
            return scope[name]
        entry = SymbolEntry(
            name=name, kind=kind, type=type_, line=line,
            scope_level=len(self.scopes) - 1, is_array=is_array,
            array_size=array_size, array_dims=array_dims, params=params,
        )
        scope[name] = entry
        self.all_symbols.append(entry)
        return entry

    def _lookup(self, name) -> Optional[SymbolEntry]:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return self.builtins.get(name)

    def _error(self, message, line):
        self.diagnostics.append(Diagnostic("error", message, line))

    def _warning(self, message, line):
        self.diagnostics.append(Diagnostic("warning", message, line))

    # -- structs/unions ------------------------------------------------------
    def _register_struct_tag(self, node: StructDecl):
        kind = "union" if node.is_union else "struct"
        if node.tag in self.structs:
            self._error(
                f"redefinition of '{kind} {node.tag}' (previously declared at line "
                f"{self.structs[node.tag].line})",
                node.line,
            )
            return
        self.structs[node.tag] = StructInfo(tag=node.tag, is_union=node.is_union, line=node.line)

    def _fill_struct_fields(self, node: StructDecl):
        info = self.structs.get(node.tag)
        if info is None or info.line != node.line:
            return   # a redefinition; the tag already belongs to the first one
        seen = set()
        for f in node.fields:
            if f.name in seen:
                self._error(f"duplicate member '{f.name}' in struct '{node.tag}'", f.line)
                continue
            seen.add(f.name)
            self._check_type_exists(f.type, f.line)
            info.fields[f.name] = f.type
            info.field_order.append(f.name)
            info.field_array_dims[f.name] = f.array_dims

    def _check_type_exists(self, type_str: str, line: int):
        """Reports an undeclared-tag error for a `struct Foo`/`union Foo`
        type (any pointer depth) used before that tag was declared."""
        if is_struct_type(type_str) and struct_tag(type_str) not in self.structs:
            kind = "union" if type_str.rstrip("*").startswith("union ") else "struct"
            self._error(f"use of undeclared {kind} '{struct_tag(type_str)}'", line)

    # -- entry point -------------------------------------------------------
    def analyze(self, program: Program):
        # pass 1: register every function signature and struct/union tag
        # first, so forward references (a call to a function defined
        # later, or a struct that points to another declared later)
        # still resolve.
        seen_funcs: Dict[str, FuncDecl] = {}
        for decl in program.declarations:
            if isinstance(decl, FuncDecl):
                prev = seen_funcs.get(decl.name)
                if prev is not None:
                    # A prototype and the definition (or two prototypes)
                    # of the same function are fine in C as long as the
                    # signatures agree; two bodies, or a mismatch, aren't.
                    same_sig = (prev.return_type == decl.return_type and
                                [p.type for p in prev.params] == [p.type for p in decl.params])
                    if not same_sig:
                        self._error(
                            f"conflicting types for function '{decl.name}' "
                            f"(previously declared at line {prev.line})", decl.line)
                    elif prev.body is not None and decl.body is not None:
                        self._error(
                            f"redefinition of function '{decl.name}' "
                            f"(previously defined at line {prev.line})", decl.line)
                    if decl.body is not None:
                        seen_funcs[decl.name] = decl
                    continue
                seen_funcs[decl.name] = decl
                self._declare(
                    decl.name, "func", decl.return_type, decl.line,
                    params=[p.type for p in decl.params],
                )
            elif isinstance(decl, StructDecl):
                self._register_struct_tag(decl)

        for decl in program.declarations:
            if isinstance(decl, StructDecl):
                self._fill_struct_fields(decl)
        for decl in program.declarations:
            if isinstance(decl, FuncDecl):
                self._check_type_exists(decl.return_type, decl.line)
                for p in decl.params:
                    self._check_type_exists(p.type, p.line)

        for decl in program.declarations:
            if isinstance(decl, FuncDecl):
                self._visit_function(decl)
            elif isinstance(decl, StructDecl):
                pass   # already fully handled above
            else:
                self._visit_stmt(decl)

        self._report_unused(self.scopes[0])
        return self.diagnostics

    # -- functions ---------------------------------------------------------
    def _visit_function(self, node: FuncDecl):
        self.current_function_return_type = node.return_type
        self._push_scope()
        for p in node.params:
            self._declare(p.name, "param", p.type, p.line)
        if node.body is not None:
            for stmt in node.body.statements:
                self._visit_stmt(stmt)
            self._report_unused(self.scopes[-1])
        self._pop_scope()
        self.current_function_return_type = None

    def _report_unused(self, scope: Dict[str, SymbolEntry]):
        for name, entry in scope.items():
            if entry.kind in ("var", "array") and not entry.used:
                self._warning(f"variable '{name}' is declared but never used", entry.line)

    # -- statements ----------------------------------------------------
    def _visit_stmt(self, node):
        if node is None:
            return
        if isinstance(node, VarDecl):
            self._visit_var_decl(node)
        elif isinstance(node, DeclGroup):
            for d in node.decls:
                self._visit_var_decl(d)
        elif isinstance(node, Block):
            self._push_scope()
            for s in node.statements:
                self._visit_stmt(s)
            self._report_unused(self.scopes[-1])
            self._pop_scope()
        elif isinstance(node, If):
            self._visit_expr(node.cond)
            self._visit_stmt(node.then_branch)
            self._visit_stmt(node.else_branch)
        elif isinstance(node, While):
            self._visit_expr(node.cond)
            self._visit_stmt(node.body)
        elif isinstance(node, For):
            self._push_scope()
            self._visit_stmt(node.init)
            if node.cond is not None:
                self._visit_expr(node.cond)
            if node.update is not None:
                self._visit_expr(node.update)
            self._visit_stmt(node.body)
            self._pop_scope()
        elif isinstance(node, Return):
            if node.value is not None:
                self._visit_expr(node.value)
                if self.current_function_return_type == "void":
                    self._error("returning a value from a void function", node.line)
            else:
                if self.current_function_return_type not in (None, "void"):
                    self._warning(
                        f"function should return a value of type "
                        f"'{self.current_function_return_type}'",
                        node.line,
                    )
        elif isinstance(node, (Break, Continue)):
            pass
        elif isinstance(node, ExprStmt):
            self._visit_expr(node.expr)
        elif isinstance(node, StructDecl):
            self._register_struct_tag(node)
            self._fill_struct_fields(node)
        else:
            raise TypeError(f"semantic analyzer: unhandled statement {type(node)}")

    def _visit_var_decl(self, node: VarDecl):
        self._check_type_exists(node.type, node.line)
        is_array = node.array_size is not None
        at_file_scope = len(self.scopes) == 1
        init_type = None

        if isinstance(node.init, InitList):
            if is_array:
                self._check_array_initializer(node, at_file_scope)
            else:
                self._visit_init_items(node.init.items)
                self._error(
                    f"brace-enclosed initialiser for non-array variable '{node.name}' "
                    f"is not supported by this tool (initialise it with an expression)",
                    node.line,
                )
        elif is_array and node.init is not None:
            if (isinstance(node.init, Literal) and node.init.type == "string"
                    and node.type == "char" and not node.array_dims):
                length = string_literal_length(node.init.value)
                if node.unsized:
                    node.array_size = length + 1
                elif length > node.array_size:
                    self._error(
                        f"initialiser string for array '{node.name}' is too long "
                        f"({length} characters, array holds {node.array_size})",
                        node.line,
                    )
            else:
                self._visit_expr(node.init)
                self._error(
                    f"array '{node.name}' must be initialised with a brace-enclosed list "
                    f"(e.g. {{1, 2, 3}})" + (" or a string literal" if node.type == "char" else ""),
                    node.line,
                )
        elif node.init is not None:
            init_type = self._visit_expr(node.init)
            if at_file_scope and not is_constant_expression(node.init):
                self._error(
                    f"initialiser for global '{node.name}' is not a compile-time constant",
                    node.line,
                )

        if node.unsized and not node.array_size:
            self._error(
                f"size of array '{node.name}' is unknown: give a size or an initialiser",
                node.line,
            )
            node.array_size = 1   # keep later bookkeeping well-formed
            if node.array_dims:
                node.array_dims[0] = 1

        entry = self._declare(
            node.name, "array" if is_array else "var",
            node.type, node.line, is_array=is_array,
            array_size=node.array_size, array_dims=node.array_dims,
        )

        if init_type is not None:
            self._check_assignable(node.type, init_type, node.line, node.name)

    def _visit_init_items(self, items):
        for item in items:
            if isinstance(item, InitList):
                self._visit_init_items(item.items)
            else:
                self._visit_expr(item)

    def _check_array_initializer(self, node: VarDecl, at_file_scope: bool):
        """Lays a brace-enclosed initialiser out over the array the way C
        does (row-major; a nested '{...}' starts at the next row
        boundary and fills that row/sub-array; missing elements are
        zero), type-checks every element against the element type, and
        stores the result as `node.init.layout` -- (flat_index, expr)
        pairs -- for the IR generator. Also infers the first dimension
        for `int a[] = {...}` / `int a[][3] = {...}`."""
        dims = list(node.array_dims) if node.array_dims else [node.array_size]
        name = node.name
        layout = []
        reported = set()

        def sub_size(level):
            size = 1
            for d in dims[level + 1:]:
                size *= d
            return size

        def error_once(key, msg):
            if key not in reported:
                reported.add(key)
                self._error(msg, node.line)

        def fill(items, level, base, size):
            """size is None for the (unsized) outermost level."""
            cur = base
            for item in items:
                if size is not None and cur - base >= size:
                    error_once("excess", f"too many elements in initialiser for array '{name}'")
                    self._visit_init_items([item])
                    continue
                if isinstance(item, InitList):
                    if level + 1 >= len(dims):
                        error_once("braces", f"too many braces in initialiser for array '{name}' "
                                             f"(a nested '{{...}}' needs another array dimension)")
                        self._visit_init_items(item.items)
                        cur += 1
                        continue
                    row = sub_size(level)
                    offset = cur - base
                    if offset % row:
                        cur = base + (offset // row + 1) * row
                        if size is not None and cur - base >= size:
                            error_once("excess", f"too many elements in initialiser for array '{name}'")
                            self._visit_init_items(item.items)
                            continue
                    fill(item.items, level + 1, cur, row)
                    cur += row
                else:
                    item_type = self._visit_expr(item)
                    if item_type is not None:
                        self._check_assignable(node.type, item_type, item.line,
                                               f"initialiser of '{name}'")
                    if at_file_scope and not is_constant_expression(item):
                        error_once("const", f"initialiser element for global array '{name}' "
                                            f"is not a compile-time constant")
                    layout.append((cur, item))
                    cur += 1
            return cur

        total = 1
        for d in dims:
            total *= d
        extent = fill(node.init.items, 0, 0, None if node.unsized else total)

        if node.unsized:
            row = sub_size(0)
            first = -(-extent // row) if extent else 0
            if first == 0:
                self._error(f"size of array '{name}' cannot be zero "
                            f"(empty initialiser and no size given)", node.line)
                first = 1
            node.array_size = first
            if node.array_dims:
                node.array_dims[0] = first
        node.init.layout = layout

    # -- expressions: returns the inferred type as a string -----------
    def _visit_expr(self, node) -> Optional[str]:
        if node is None:
            return None

        if isinstance(node, Literal):
            return "null" if node.type == "null" else node.type

        if isinstance(node, Identifier):
            entry = self._lookup(node.name)
            if entry is None:
                self._error(f"use of undeclared identifier '{node.name}'", node.line)
                return None
            entry.used = True
            return entry.type

        if isinstance(node, ArrayAccess):
            result_type = None
            base_type, base_dims, base_is_array = self._resolve_indexable_base(node.base)
            base_desc = self._describe_base(node.base)
            if base_type is None:
                pass   # the base itself already reported why
            elif not base_is_array and not is_pointer_type(base_type):
                self._error(f"'{base_desc}' is not an array or pointer", node.line)
            else:
                n_given = len(node.indices)
                if base_is_array:
                    declared_dims = base_dims or [1]
                    if n_given > len(declared_dims):
                        self._error(
                            f"too many indices for array '{base_desc}' (declared "
                            f"with {len(declared_dims)} dimension(s), {n_given} given)",
                            node.line,
                        )
                    elif n_given < len(declared_dims):
                        # a[i] on a 2-D+ array ("give me row i") is valid C but
                        # would need a partial-array type this tool doesn't
                        # model -- reported clearly rather than miscompiled.
                        self._error(
                            f"partial array indexing is not supported: '{base_desc}' "
                            f"needs {len(declared_dims)} index(es), {n_given} given",
                            node.line,
                        )
                    else:
                        result_type = base_type
                else:
                    # pointer indexing: each [idx] strips one level of
                    # pointer-ness, so int** indexed twice is int.
                    t = base_type
                    for _ in range(n_given):
                        if not is_pointer_type(t):
                            self._error(f"too many indices for pointer '{base_desc}'", node.line)
                            t = None
                            break
                        t = pointer_base(t)
                    result_type = t
            for idx in node.indices:
                index_type = self._visit_expr(idx)
                if index_type is not None and index_type not in ("int", "char"):
                    self._warning("array index should be an integer expression", node.line)
            return result_type

        if isinstance(node, Call):
            entry = self._lookup(node.name)
            arg_types = [self._visit_expr(a) for a in node.args]
            if entry is None:
                self._error(f"call to undeclared function '{node.name}'", node.line)
                return None
            if entry.kind != "func":
                self._error(f"'{node.name}' is not a function", node.line)
                return None
            entry.used = True
            if entry.params is not None and len(entry.params) != len(node.args):
                self._error(
                    f"'{node.name}' expects {len(entry.params)} argument(s) "
                    f"but {len(node.args)} given",
                    node.line,
                )
            return entry.type

        if isinstance(node, Assign):
            value_type = self._visit_expr(node.value)
            target_type = self._visit_target(node.target)
            if target_type is not None and value_type is not None:
                self._check_assignable(target_type, value_type, node.line, "assignment")
            return target_type

        if isinstance(node, BinOp):
            left_type = self._visit_expr(node.left)
            right_type = self._visit_expr(node.right)
            return self._check_binop(node.op, left_type, right_type, node.line)

        if isinstance(node, UnaryOp):
            if node.op == "&":
                if not isinstance(node.operand, (Identifier, ArrayAccess, MemberAccess)):
                    self._error("cannot take the address of a non-lvalue expression", node.line)
                    self._visit_expr(node.operand)
                    return None
                operand_type = self._visit_expr(node.operand)
                return operand_type + "*" if operand_type is not None else None
            if node.op == "*":
                operand_type = self._visit_expr(node.operand)
                if operand_type is None:
                    return None
                if not is_pointer_type(operand_type):
                    self._error(f"cannot dereference non-pointer type '{operand_type}'", node.line)
                    return None
                return pointer_base(operand_type)
            operand_type = self._visit_expr(node.operand)
            if node.op in ("++", "--") and not isinstance(
                node.operand, (Identifier, ArrayAccess, MemberAccess)
            ):
                self._error(f"'{node.op}' requires a variable operand", node.line)
            return operand_type

        if isinstance(node, Cast):
            self._visit_expr(node.operand)
            return node.type

        if isinstance(node, Sizeof):
            if node.expr is not None:
                self._visit_expr(node.expr)
            return "int"

        if isinstance(node, MemberAccess):
            return self._visit_member_access(node)

        raise TypeError(f"semantic analyzer: unhandled expression {type(node)}")

    def _resolve_member_field(self, node: MemberAccess):
        """Shared by `_visit_member_access` (member used as a plain
        value) and `_resolve_indexable_base` (member used as the base
        of `[...]`, e.g. a struct field that's itself an array).
        Returns (field_type, field_array_dims) or (None, None) on
        error -- the error itself is already reported."""
        base_type = self._visit_expr(node.base)
        if base_type is None:
            return None, None

        if node.arrow:
            if not is_pointer_type(base_type) or not is_struct_type(pointer_base(base_type)):
                self._error(
                    f"'->' requires a pointer to a struct/union, got '{base_type}'", node.line,
                )
                return None, None
            struct_type_str = pointer_base(base_type)
        else:
            if not is_struct_type(base_type) or is_pointer_type(base_type):
                self._error(
                    f"'.' requires a struct/union value (use '->' for a pointer), "
                    f"got '{base_type}'", node.line,
                )
                return None, None
            struct_type_str = base_type

        tag = struct_tag(struct_type_str)
        info = self.structs.get(tag)
        if info is None:
            self._error(f"use of undeclared struct/union '{tag}'", node.line)
            return None, None
        if node.member not in info.fields:
            kind = "union" if info.is_union else "struct"
            self._error(f"{kind} '{tag}' has no member '{node.member}'", node.line)
            return None, None
        return info.fields[node.member], info.field_array_dims.get(node.member)

    def _visit_member_access(self, node: MemberAccess) -> Optional[str]:
        field_type, field_dims = self._resolve_member_field(node)
        if field_type is None:
            return None
        if field_dims:
            self._error(f"array field '{node.member}' must be indexed", node.line)
            return None
        return field_type

    def _resolve_indexable_base(self, node):
        """Returns (type, array_dims, is_array) for whatever sits to
        the left of `[...]`: a plain variable (array or pointer), or a
        struct/union member access whose field is itself an array
        (`b.vals[i]`, `p->vals[i]`) -- including through another
        indexing step first (`boxes[i].vals[j]`)."""
        if isinstance(node, Identifier):
            entry = self._lookup(node.name)
            if entry is None:
                self._error(f"use of undeclared identifier '{node.name}'", node.line)
                return None, None, False
            entry.used = True
            return entry.type, entry.array_dims, entry.is_array
        if isinstance(node, MemberAccess):
            field_type, field_dims = self._resolve_member_field(node)
            if field_type is None:
                return None, None, False
            return field_type, field_dims, bool(field_dims)
        # anything else (another ArrayAccess, a dereference, a call
        # result, ...) is just a scalar/pointer-typed value with no
        # array dimensions of its own.
        return self._visit_expr(node), None, False

    def _describe_base(self, node) -> str:
        """User-facing name for an ArrayAccess's base, for diagnostics."""
        if isinstance(node, Identifier):
            return node.name
        if isinstance(node, MemberAccess):
            return f"{self._describe_base(node.base)}{'->' if node.arrow else '.'}{node.member}"
        if isinstance(node, ArrayAccess):
            return self._describe_base(node.base) + "[...]" * len(node.indices)
        return "expression"

    def _visit_target(self, node) -> Optional[str]:
        """Like _visit_expr, but for the left-hand side of an assignment."""
        if isinstance(node, Identifier):
            entry = self._lookup(node.name)
            if entry is None:
                self._error(f"use of undeclared identifier '{node.name}'", node.line)
                return None
            if entry.kind == "func":
                self._error(f"cannot assign to function '{node.name}'", node.line)
                return None
            entry.used = True
            return entry.type
        if isinstance(node, ArrayAccess):
            return self._visit_expr(node)
        if isinstance(node, UnaryOp) and node.op == "*":
            return self._visit_expr(node)
        if isinstance(node, MemberAccess):
            return self._visit_expr(node)
        self._error("invalid assignment target", getattr(node, "line", 0))
        return None

    # -- type-compatibility helpers ---------------------------------------
    def _check_assignable(self, target_type, value_type, line, context):
        if target_type is None or value_type is None:
            return
        if target_type == value_type:
            return

        if value_type == "null":
            if is_pointer_type(target_type):
                return
            self._error(f"cannot assign 'NULL' to non-pointer type '{target_type}'", line)
            return

        if is_pointer_type(target_type) and is_pointer_type(value_type):
            # void* converts silently to/from any pointer type, as in C;
            # any other mismatched pointer types are a likely bug, but
            # only worth a warning at this tool's scope.
            if target_type == "void*" or value_type == "void*":
                return
            self._warning(
                f"implicit conversion from incompatible pointer type '{value_type}' "
                f"to '{target_type}' in {context}",
                line,
            )
            return

        if is_pointer_type(target_type) and value_type in NUMERIC_TYPES:
            self._warning(
                f"implicit conversion from '{value_type}' to pointer type "
                f"'{target_type}' in {context}",
                line,
            )
            return

        if is_pointer_type(value_type) and target_type in NUMERIC_TYPES:
            self._warning(
                f"implicit conversion from pointer type '{value_type}' to "
                f"'{target_type}' in {context}",
                line,
            )
            return

        if target_type in NUMERIC_TYPES and value_type in NUMERIC_TYPES:
            # implicit numeric conversion is legal in C but can lose
            # precision -- surfaced as a warning, not an error.
            if target_type in ("int", "char") and value_type in ("float", "double"):
                self._warning(
                    f"implicit conversion from '{value_type}' to '{target_type}' "
                    f"in {context} may lose precision",
                    line,
                )
            return
        self._error(
            f"type mismatch in {context}: cannot assign '{value_type}' to '{target_type}'",
            line,
        )

    def _check_binop(self, op, left_type, right_type, line) -> Optional[str]:
        if left_type is None or right_type is None:
            return None

        if op in ("&&", "||"):
            return "int"

        left_ptr = is_pointer_type(left_type) or left_type == "null"
        right_ptr = is_pointer_type(right_type) or right_type == "null"
        if left_ptr or right_ptr:
            if op in ("==", "!=", "<", ">", "<=", ">="):
                if not (left_ptr or left_type in NUMERIC_TYPES) or \
                   not (right_ptr or right_type in NUMERIC_TYPES):
                    self._error(f"cannot compare '{left_type}' with '{right_type}'", line)
                return "int"
            if op == "+":
                if is_pointer_type(left_type) and right_type in NUMERIC_TYPES:
                    return left_type
                if is_pointer_type(right_type) and left_type in NUMERIC_TYPES:
                    return right_type
                self._error(f"invalid operand types '{left_type}' and '{right_type}' for '+'", line)
                return None
            if op == "-":
                if is_pointer_type(left_type) and right_type in NUMERIC_TYPES:
                    return left_type
                if is_pointer_type(left_type) and is_pointer_type(right_type):
                    return "int"
                self._error(f"invalid operand types '{left_type}' and '{right_type}' for '-'", line)
                return None
            self._error(f"invalid operand types '{left_type}' and '{right_type}' for '{op}'", line)
            return None

        if op in ("==", "!=", "<", ">", "<=", ">="):
            if left_type not in NUMERIC_TYPES or right_type not in NUMERIC_TYPES:
                self._error(
                    f"cannot compare '{left_type}' with '{right_type}'", line
                )
            return "int"

        if left_type == "string" or right_type == "string":
            self._error(f"invalid operand type 'string' for operator '{op}'", line)
            return None

        if left_type not in NUMERIC_TYPES or right_type not in NUMERIC_TYPES:
            self._error(
                f"invalid operand types '{left_type}' and '{right_type}' for '{op}'",
                line,
            )
            return None

        # simple numeric promotion: float/double "wins" over int/char
        if "double" in (left_type, right_type):
            return "double"
        if "float" in (left_type, right_type):
            return "float"
        return "int"
