"""
AST Node Definitions
--------------------
Every node carries the source line it came from, so later phases
(semantic analysis, IR generation) can report diagnostics that point
back to the original C file.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Any


@dataclass
class Node:
    line: int = 0


@dataclass
class Program(Node):
    declarations: List[Node] = field(default_factory=list)


@dataclass
class Param(Node):
    type: str = ""
    name: str = ""


@dataclass
class StructField(Node):
    """One member of a struct/union, e.g. `int x;` or `struct Point *next;`."""
    type: str = ""
    name: str = ""
    array_dims: Optional[List[int]] = None


@dataclass
class StructDecl(Node):
    """`struct Point { int x; int y; };` or the `union` equivalent.
    Declares the tag (`Point`); doesn't itself declare a variable."""
    tag: str = ""
    fields: List[StructField] = field(default_factory=list)
    is_union: bool = False


@dataclass
class FuncDecl(Node):
    return_type: str = ""
    name: str = ""
    params: List[Param] = field(default_factory=list)
    body: Optional["Block"] = None


@dataclass
class VarDecl(Node):
    """A single declared variable, e.g. `int x = 5;` or `int arr[10];`
    or `int grid[2][3];`. `array_size` stays the first dimension (or
    the only one) for anything that only ever cared about 1-D arrays;
    `array_dims` additionally holds every dimension, and is set only
    when there's more than one (len(array_dims) > 1)."""
    type: str = ""
    name: str = ""
    array_size: Optional[int] = None
    array_dims: Optional[List[int]] = None
    init: Optional[Node] = None
    # `int a[] = {...}` / `char s[] = "..."`: the first dimension was left
    # empty in the source and is filled in by semantic analysis from the
    # initialiser (array_size holds a placeholder 0 until then).
    unsized: bool = False


@dataclass
class InitList(Node):
    """A brace-enclosed initialiser, e.g. the `{10, 20, 30}` in
    `int a[3] = {10, 20, 30};`. Items are expressions or nested
    InitLists (`{{1, 2}, {3, 4}}` for a 2-D array). Only valid as the
    initialiser of an array declaration -- semantic analysis rejects it
    anywhere else, and lays it out row-major into `layout` (a list of
    (flat_index, expr) pairs) for the IR generator."""
    items: List[Node] = field(default_factory=list)
    layout: Optional[List] = None


@dataclass
class DeclGroup(Node):
    """`int a, b, c;` -- several declarators sharing one declaration.
    Unlike Block, this does NOT open a new scope."""
    decls: List[Node] = field(default_factory=list)


@dataclass
class Block(Node):
    statements: List[Node] = field(default_factory=list)


@dataclass
class If(Node):
    cond: Optional[Node] = None
    then_branch: Optional[Node] = None
    else_branch: Optional[Node] = None


@dataclass
class While(Node):
    cond: Optional[Node] = None
    body: Optional[Node] = None


@dataclass
class For(Node):
    init: Optional[Node] = None
    cond: Optional[Node] = None
    update: Optional[Node] = None
    body: Optional[Node] = None


@dataclass
class Return(Node):
    value: Optional[Node] = None


@dataclass
class Break(Node):
    pass


@dataclass
class Continue(Node):
    pass


@dataclass
class ExprStmt(Node):
    expr: Optional[Node] = None


@dataclass
class Assign(Node):
    op: str = "="          # =, +=, -=, *=, /=, %=
    target: Optional[Node] = None
    value: Optional[Node] = None


@dataclass
class BinOp(Node):
    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None


@dataclass
class UnaryOp(Node):
    op: str = ""
    operand: Optional[Node] = None
    postfix: bool = False   # True for x++ / x--, False for ++x / --x / -x / !x


@dataclass
class Literal(Node):
    type: str = ""          # int, float, char, string
    value: Any = None


@dataclass
class Identifier(Node):
    name: str = ""


@dataclass
class ArrayAccess(Node):
    """`a[i]` (indices=[i]) or `a[i][j]` (indices=[i, j]) -- any number
    of chained indices applied to `base`. `base` is usually a plain
    `Identifier` (`a[i]`), but can be any other postfix expression,
    e.g. a `MemberAccess` for a struct field that's itself an array
    (`b.vals[i]`, `p->vals[i]`) or another `ArrayAccess` for an array
    of structs each holding an array field (`boxes[i].vals[j]`)."""
    base: Optional[Node] = None
    indices: List[Node] = field(default_factory=list)


@dataclass
class MemberAccess(Node):
    """`s.x` (arrow=False) or `p->x` (arrow=True). `base` can itself be
    any expression, including another MemberAccess, so `a.b.c` and
    `p->next->val` are just nested MemberAccess nodes."""
    base: Optional[Node] = None
    member: str = ""
    arrow: bool = False


@dataclass
class Call(Node):
    name: str = ""
    args: List[Node] = field(default_factory=list)


@dataclass
class Cast(Node):
    """`(int*) expr` -- an explicit type cast. `type` already has any
    pointer stars baked in, e.g. "int*", "char**"."""
    type: str = ""
    operand: Optional[Node] = None


@dataclass
class Sizeof(Node):
    """`sizeof(type)` or `sizeof(expr)` -- exactly one of `type` /
    `expr` is set. Lowered straight to C's own `sizeof(...)` rather
    than evaluated here, so the real sizes (including padding, on a
    real target) come from the C compiler that builds the output."""
    type: Optional[str] = None
    expr: Optional[Node] = None
