"""
Syntax Analyser
---------------
A recursive-descent parser for the C subset handled by this tool. It
builds an abstract syntax tree (AST) from the token stream produced by
the lexer and checks the input against the grammar described in
GRAMMAR.md.

Error recovery uses the classic "panic mode": when a statement or
top-level declaration fails to parse, the parser reports the error
with its line number and skips tokens until it finds a safe
re-synchronisation point, so one bad statement doesn't stop the rest
of the file from being checked. Re-synchronisation is brace-aware: a
'{ ... }' group met while skipping (an initialiser list, a nested
block) is skipped as a whole, so its closing '}' is never mistaken for
the end of the enclosing function body -- which would otherwise make
every following statement look like a (bogus) global declaration and
produce a cascade of misleading errors. Skipping also stops early, at
the start of the next line, if that line begins with a keyword that
can only start a new statement (`for`, `return`, a type name, ...), so
a single missing ';' costs one error, not two.
"""

from typing import List, Optional

from .lexer import Token
from .ast_nodes import (
    Program, Param, FuncDecl, VarDecl, DeclGroup, Block, If, While, For, Return,
    Break, Continue, ExprStmt, Assign, BinOp, UnaryOp, Literal,
    Identifier, ArrayAccess, Call, Cast, Sizeof, StructDecl, StructField, MemberAccess,
    InitList,
)

TYPE_KEYWORDS = {"int", "float", "char", "double", "void"}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%="}
# Keywords that can only begin a statement/declaration -- safe points
# to resume parsing after an error.
STATEMENT_START_KEYWORDS = TYPE_KEYWORDS | {
    "struct", "union", "if", "while", "for", "return", "break", "continue",
}


def _describe(tok) -> str:
    """How a token is named in an error message."""
    if tok.kind == "EOF":
        return "end of file"
    return f"'{tok.value or tok.kind}'"


class _TypeTok:
    """A stand-in for a plain Token after consuming a (possibly
    two-keyword) base type, so every existing call site that reads
    `.value`/`.line`/`.col` off what `self._advance()` used to return
    for a bare type keyword keeps working unchanged."""
    __slots__ = ("value", "line", "col")

    def __init__(self, value: str, line: int, col: int):
        self.value = value
        self.line = line
        self.col = col


class ParseError(Exception):
    def __init__(self, message: str, line: int, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col

    def __str__(self):
        col_suffix = f" [col {self.col}]" if self.col else ""
        return f"Syntax Error (line {self.line}){col_suffix}: {self.message}"


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0
        self.errors: List[ParseError] = []

    # -- token stream helpers --------------------------------------------
    def _peek(self, offset: int = 0) -> Token:
        i = self.pos + offset
        if i >= len(self.tokens):
            return self.tokens[-1]  # EOF
        return self.tokens[i]

    def _advance(self) -> Token:
        tok = self._peek()
        if tok.kind != "EOF":
            self.pos += 1
        return tok

    def _check(self, kind: str, value: Optional[str] = None) -> bool:
        tok = self._peek()
        if tok.kind != kind:
            return False
        if value is not None and tok.value != value:
            return False
        return True

    def _match(self, kind: str, value: Optional[str] = None) -> Optional[Token]:
        if self._check(kind, value):
            return self._advance()
        return None

    def _expect(self, kind: str, value: Optional[str] = None) -> Token:
        if self._check(kind, value):
            return self._advance()
        tok = self._peek()
        expected = value if value else {"IDENT": "an identifier",
                                        "INT_CONST": "an integer constant"}.get(kind, kind)
        if value:
            expected = f"'{expected}'"
        raise ParseError(f"expected {expected} but found {_describe(tok)}", tok.line, tok.col)

    def _token_starts_type(self, tok: Token) -> bool:
        return tok.kind == "KEYWORD" and (tok.value in TYPE_KEYWORDS or tok.value in ("struct", "union"))

    def _is_type_keyword(self) -> bool:
        return self._token_starts_type(self._peek())

    def _looks_like_struct_decl(self) -> bool:
        """True at `struct Tag {` / `union Tag {` -- a tag *declaration*,
        as opposed to `struct Tag name;`, a variable of that type."""
        return (self._check("KEYWORD", "struct") or self._check("KEYWORD", "union")) \
            and self._peek(1).kind == "IDENT" \
            and self._peek(2).kind == "PUNCT" and self._peek(2).value == "{"

    def _parse_base_type(self) -> _TypeTok:
        """Consumes a base type with no pointer stars yet: a builtin
        keyword, or `struct TAG` / `union TAG`."""
        if self._check("KEYWORD", "struct") or self._check("KEYWORD", "union"):
            kw = self._advance()
            tag_tok = self._expect("IDENT")
            return _TypeTok(f"{kw.value} {tag_tok.value}", kw.line, kw.col)
        tok = self._advance()
        return _TypeTok(tok.value, tok.line, tok.col)

    def _parse_struct_decl(self) -> StructDecl:
        kw = self._advance()   # 'struct' or 'union'
        is_union = kw.value == "union"
        tag_tok = self._expect("IDENT")
        self._expect("PUNCT", "{")
        fields: List[StructField] = []
        while not self._check("PUNCT", "}") and not self._check("EOF"):
            if not self._is_type_keyword():
                tok = self._peek()
                raise ParseError(
                    f"expected a field type, found '{tok.value or tok.kind}'",
                    tok.line, tok.col,
                )
            field_type = self._parse_base_type()
            depth = self._consume_stars()
            field_name_tok = self._expect("IDENT")
            dims = []
            while self._match("PUNCT", "["):
                size_tok = self._expect("INT_CONST")
                dims.append(int(size_tok.value))
                self._expect("PUNCT", "]")
            self._expect("PUNCT", ";")
            fields.append(StructField(
                line=field_name_tok.line,
                type=field_type.value + "*" * depth,
                name=field_name_tok.value,
                array_dims=dims or None,
            ))
        self._expect("PUNCT", "}")
        self._expect("PUNCT", ";")
        return StructDecl(line=kw.line, tag=tag_tok.value, fields=fields, is_union=is_union)

    def _consume_stars(self) -> int:
        """Consumes zero or more '*' tokens after a base type, for
        pointer declarators (`int *p`), casts (`(int*)x`), and
        `sizeof` type names (`sizeof(char*)`)."""
        depth = 0
        while self._match("OP", "*"):
            depth += 1
        return depth

    # -- synchronisation for panic-mode recovery -------------------------
    def _skip_balanced_braces(self):
        """At a '{': consumes through its matching '}' (or to EOF)."""
        depth = 0
        while not self._check("EOF"):
            tok = self._advance()
            if tok.kind == "PUNCT" and tok.value == "{":
                depth += 1
            elif tok.kind == "PUNCT" and tok.value == "}":
                depth -= 1
                if depth == 0:
                    return

    def _at_statement_boundary(self, stmt_start: int) -> bool:
        """True at a keyword that can only begin a statement, sitting
        first on its line (the previous token is on an earlier line),
        and not the very token the failed statement started at (so
        recovery always makes progress)."""
        tok = self._peek()
        if tok.kind != "KEYWORD" or tok.value not in STATEMENT_START_KEYWORDS:
            return False
        if self.pos == stmt_start or self.pos == 0:
            return False
        return self.tokens[self.pos - 1].line < tok.line

    def _synchronize_statement(self, stmt_start: int):
        """Skips the rest of a statement that failed to parse.

        Stops after the next ';'; or just before the '}' that closes the
        enclosing block; or just before a statement-only keyword that
        starts a new line (so `int a = 5` <newline> `int b = 10;` -- a
        missing ';' -- reports one error and still parses `b`). Any
        '{ ... }' met on the way is skipped as one unit: its '}' belongs
        to it, not to the enclosing block. Likewise any '{' the failed
        statement itself had already opened (an error part-way through
        an initialiser list) is closed out first, so that list's '}' is
        not mistaken for the end of the enclosing block either."""
        still_open = 0
        for t in self.tokens[stmt_start:self.pos]:
            if t.kind == "PUNCT" and t.value == "{":
                still_open += 1
            elif t.kind == "PUNCT" and t.value == "}" and still_open:
                still_open -= 1
        while not self._check("EOF"):
            tok = self._peek()
            if still_open:
                if tok.kind == "PUNCT" and tok.value == "}":
                    still_open -= 1
                elif tok.kind == "PUNCT" and tok.value == "{":
                    still_open += 1
                self._advance()
                continue
            if self._at_statement_boundary(stmt_start):
                return
            if tok.kind == "PUNCT" and tok.value == ";":
                self._advance()
                return
            if tok.kind == "PUNCT" and tok.value == "}":
                return
            if tok.kind == "PUNCT" and tok.value == "{":
                self._skip_balanced_braces()
                continue
            self._advance()

    def _synchronize_top_level(self, decl_start: Optional[int] = None):
        depth = 0
        while not self._check("EOF"):
            if depth == 0 and decl_start is not None and self._at_statement_boundary(decl_start) \
                    and self._is_type_keyword():
                return
            if self._check("PUNCT", "{"):
                depth += 1
                self._advance()
                continue
            if self._check("PUNCT", "}"):
                self._advance()
                if depth == 0:
                    return
                depth -= 1
                if depth == 0:
                    return
                continue
            if self._check("PUNCT", ";") and depth == 0:
                self._advance()
                return
            self._advance()

    # -- entry point -------------------------------------------------------
    def parse(self) -> Program:
        decls = []
        while not self._check("EOF"):
            decl_start = self.pos
            try:
                decls.append(self._parse_top_level())
            except ParseError as e:
                self.errors.append(e)
                self._synchronize_top_level(decl_start)
                if self.pos == decl_start:
                    self._advance()   # guarantee progress
        return Program(line=1, declarations=decls)

    def _parse_top_level(self):
        if self._looks_like_struct_decl():
            return self._parse_struct_decl()

        if not self._is_type_keyword():
            tok = self._peek()
            raise ParseError(
                f"expected a type keyword to start a declaration, found "
                f"'{tok.value or tok.kind}'",
                tok.line, tok.col,
            )

        type_tok = self._parse_base_type()
        pointer_depth = self._consume_stars()
        name_tok = self._expect("IDENT")

        if self._check("PUNCT", "("):
            return self._parse_function(type_tok, name_tok, pointer_depth)
        return self._parse_global_var(type_tok, name_tok, pointer_depth)

    # -- functions -------------------------------------------------------
    def _parse_function(self, type_tok: Token, name_tok: Token, pointer_depth: int = 0) -> FuncDecl:
        return_type = type_tok.value + "*" * pointer_depth
        self._expect("PUNCT", "(")
        params = []
        if self._check("KEYWORD", "void") and self._peek(1).kind == "PUNCT" and self._peek(1).value == ")":
            self._advance()   # a lone `(void)` means no parameters
        elif not self._check("PUNCT", ")"):
            params.append(self._parse_param())
            while self._match("PUNCT", ","):
                params.append(self._parse_param())
        self._expect("PUNCT", ")")

        if self._match("PUNCT", ";"):
            # forward declaration / prototype -- no body
            return FuncDecl(
                line=type_tok.line, return_type=return_type,
                name=name_tok.value, params=params, body=None,
            )

        body = self._parse_block()
        return FuncDecl(
            line=type_tok.line, return_type=return_type,
            name=name_tok.value, params=params, body=body,
        )

    def _parse_param(self) -> Param:
        if not self._is_type_keyword():
            tok = self._peek()
            raise ParseError(
                f"expected a parameter type, found '{tok.value or tok.kind}'",
                tok.line, tok.col,
            )
        type_tok = self._parse_base_type()
        depth = self._consume_stars()
        name_tok = self._expect("IDENT")
        return Param(line=type_tok.line, type=type_tok.value + "*" * depth, name=name_tok.value)

    # -- declarations -----------------------------------------------------
    def _parse_global_var(self, type_tok: Token, name_tok: Token, pointer_depth: int = 0):
        decls = [self._finish_one_declarator(type_tok.value + "*" * pointer_depth, name_tok)]
        while self._match("PUNCT", ","):
            depth = self._consume_stars()
            nxt = self._expect("IDENT")
            decls.append(self._finish_one_declarator(type_tok.value + "*" * depth, nxt))
        self._expect("PUNCT", ";")
        if len(decls) == 1:
            return decls[0]
        return DeclGroup(line=type_tok.line, decls=decls)

    def _finish_one_declarator(self, type_name: str, name_tok: Token) -> VarDecl:
        dims = []
        unsized = False
        while self._check("PUNCT", "["):
            bracket = self._advance()
            if self._check("PUNCT", "]"):
                # `int a[] = {...}` -- only the first dimension may be
                # left empty, and only when an initialiser supplies it
                # (checked in semantic analysis).
                if dims:
                    raise ParseError(
                        "only the first array dimension may be left empty",
                        bracket.line, bracket.col,
                    )
                unsized = True
                dims.append(0)
                self._advance()
                continue
            size_tok = self._expect("INT_CONST")
            if int(size_tok.value) <= 0:
                raise ParseError(
                    f"array size must be a positive integer, found {size_tok.value}",
                    size_tok.line, size_tok.col,
                )
            dims.append(int(size_tok.value))
            self._expect("PUNCT", "]")

        init = None
        if self._match("OP", "="):
            if self._check("PUNCT", "{"):
                init = self._parse_initializer_list()
            else:
                init = self._parse_assignment()

        return VarDecl(
            line=name_tok.line, type=type_name, name=name_tok.value,
            array_size=dims[0] if dims else None,
            array_dims=dims if len(dims) > 1 else None,
            init=init, unsized=unsized,
        )

    def _parse_initializer_list(self) -> InitList:
        """initializer-list := '{' [ item { ',' item } [ ',' ] ] '}'
           item             := initializer-list | assignment-expression

        Nested lists are allowed (`{{1, 2}, {3, 4}}`), as is a trailing
        comma. Designated initialisers (`[2] = x`, `.field = x`) are a
        real C feature this tool does not implement, so they're reported
        as such rather than as a confusing generic error."""
        brace = self._expect("PUNCT", "{")
        items = []
        while not self._check("PUNCT", "}"):
            if self._check("EOF"):
                tok = self._peek()
                raise ParseError(
                    f"missing '}}' to close the initialiser list opened at line {brace.line}",
                    tok.line, tok.col,
                )
            if self._check("PUNCT", "[") or self._check("PUNCT", "."):
                tok = self._peek()
                raise ParseError("designated initialisers ('[index] =' / '.field =') "
                                 "are not supported", tok.line, tok.col)
            if self._check("PUNCT", "{"):
                items.append(self._parse_initializer_list())
            else:
                items.append(self._parse_assignment())
            if not self._match("PUNCT", ","):
                break
        if not self._check("PUNCT", "}"):
            tok = self._peek()
            raise ParseError(
                f"expected ',' or '}}' in initialiser list but found {_describe(tok)}",
                tok.line, tok.col,
            )
        self._advance()
        return InitList(line=brace.line, items=items)

    # -- statements & blocks ------------------------------------------------
    def _parse_block(self) -> Block:
        brace = self._expect("PUNCT", "{")
        stmts = []
        while not self._check("PUNCT", "}") and not self._check("EOF"):
            stmt_start = self.pos
            try:
                stmts.append(self._parse_statement())
            except ParseError as e:
                self.errors.append(e)
                self._synchronize_statement(stmt_start)
                if self.pos == stmt_start:
                    self._advance()   # guarantee progress
        if self._check("EOF"):
            tok = self._peek()
            raise ParseError(
                f"missing '}}' to close the block opened at line {brace.line}",
                tok.line, tok.col,
            )
        self._expect("PUNCT", "}")
        return Block(line=brace.line, statements=stmts)

    def _parse_statement(self):
        if self._check("PUNCT", "{"):
            return self._parse_block()

        if self._looks_like_struct_decl():
            return self._parse_struct_decl()

        if self._is_type_keyword():
            type_tok = self._parse_base_type()
            depth = self._consume_stars()
            name_tok = self._expect("IDENT")
            return self._parse_global_var(type_tok, name_tok, depth)

        if self._check("KEYWORD", "if"):
            return self._parse_if()
        if self._check("KEYWORD", "while"):
            return self._parse_while()
        if self._check("KEYWORD", "for"):
            return self._parse_for()
        if self._check("KEYWORD", "return"):
            return self._parse_return()
        if self._check("KEYWORD", "break"):
            tok = self._advance()
            self._expect("PUNCT", ";")
            return Break(line=tok.line)
        if self._check("KEYWORD", "continue"):
            tok = self._advance()
            self._expect("PUNCT", ";")
            return Continue(line=tok.line)

        return self._parse_expr_stmt()

    def _parse_if(self) -> If:
        tok = self._expect("KEYWORD", "if")
        self._expect("PUNCT", "(")
        cond = self._parse_expr()
        self._expect("PUNCT", ")")
        then_branch = self._parse_statement()
        else_branch = None
        if self._match("KEYWORD", "else"):
            else_branch = self._parse_statement()
        return If(line=tok.line, cond=cond, then_branch=then_branch, else_branch=else_branch)

    def _parse_while(self) -> While:
        tok = self._expect("KEYWORD", "while")
        self._expect("PUNCT", "(")
        cond = self._parse_expr()
        self._expect("PUNCT", ")")
        body = self._parse_statement()
        return While(line=tok.line, cond=cond, body=body)

    def _parse_for(self) -> For:
        tok = self._expect("KEYWORD", "for")
        self._expect("PUNCT", "(")

        init = None
        if not self._check("PUNCT", ";"):
            if self._is_type_keyword():
                type_tok = self._parse_base_type()
                depth = self._consume_stars()
                name_tok = self._expect("IDENT")
                init = self._finish_one_declarator(type_tok.value + "*" * depth, name_tok)
            else:
                init = ExprStmt(line=self._peek().line, expr=self._parse_expr())
        self._expect("PUNCT", ";")

        cond = None
        if not self._check("PUNCT", ";"):
            cond = self._parse_expr()
        self._expect("PUNCT", ";")

        update = None
        if not self._check("PUNCT", ")"):
            update = self._parse_expr()
        self._expect("PUNCT", ")")

        body = self._parse_statement()
        return For(line=tok.line, init=init, cond=cond, update=update, body=body)

    def _parse_return(self) -> Return:
        tok = self._expect("KEYWORD", "return")
        value = None
        if not self._check("PUNCT", ";"):
            value = self._parse_expr()
        self._expect("PUNCT", ";")
        return Return(line=tok.line, value=value)

    def _parse_expr_stmt(self) -> ExprStmt:
        line = self._peek().line
        expr = self._parse_expr()
        self._expect("PUNCT", ";")
        return ExprStmt(line=line, expr=expr)

    # -- expressions (precedence climbing) ---------------------------------
    def _parse_expr(self):
        return self._parse_assignment()

    def _parse_assignment(self):
        left = self._parse_logical_or()
        if self._check("OP") and self._peek().value in ASSIGN_OPS:
            op_tok = self._advance()
            right = self._parse_assignment()
            return Assign(line=op_tok.line, op=op_tok.value, target=left, value=right)
        return left

    def _parse_logical_or(self):
        left = self._parse_logical_and()
        while self._check("OP", "||"):
            op_tok = self._advance()
            right = self._parse_logical_and()
            left = BinOp(line=op_tok.line, op="||", left=left, right=right)
        return left

    def _parse_logical_and(self):
        left = self._parse_equality()
        while self._check("OP", "&&"):
            op_tok = self._advance()
            right = self._parse_equality()
            left = BinOp(line=op_tok.line, op="&&", left=left, right=right)
        return left

    def _parse_equality(self):
        left = self._parse_relational()
        while self._check("OP") and self._peek().value in ("==", "!="):
            op_tok = self._advance()
            right = self._parse_relational()
            left = BinOp(line=op_tok.line, op=op_tok.value, left=left, right=right)
        return left

    def _parse_relational(self):
        left = self._parse_additive()
        while self._check("OP") and self._peek().value in ("<", ">", "<=", ">="):
            op_tok = self._advance()
            right = self._parse_additive()
            left = BinOp(line=op_tok.line, op=op_tok.value, left=left, right=right)
        return left

    def _parse_additive(self):
        left = self._parse_multiplicative()
        while self._check("OP") and self._peek().value in ("+", "-"):
            op_tok = self._advance()
            right = self._parse_multiplicative()
            left = BinOp(line=op_tok.line, op=op_tok.value, left=left, right=right)
        return left

    def _parse_multiplicative(self):
        left = self._parse_unary()
        while self._check("OP") and self._peek().value in ("*", "/", "%"):
            op_tok = self._advance()
            right = self._parse_unary()
            left = BinOp(line=op_tok.line, op=op_tok.value, left=left, right=right)
        return left

    def _parse_unary(self):
        if self._check("KEYWORD", "sizeof"):
            return self._parse_sizeof()
        if self._check("PUNCT", "(") and self._token_starts_type(self._peek(1)):
            return self._parse_cast()
        if self._check("OP") and self._peek().value in ("-", "!", "++", "--", "*", "&"):
            op_tok = self._advance()
            operand = self._parse_unary()
            return UnaryOp(line=op_tok.line, op=op_tok.value, operand=operand, postfix=False)
        return self._parse_postfix()

    def _parse_sizeof(self) -> Sizeof:
        tok = self._expect("KEYWORD", "sizeof")
        if self._check("PUNCT", "(") and self._token_starts_type(self._peek(1)):
            self._advance()  # (
            type_tok = self._parse_base_type()
            depth = self._consume_stars()
            self._expect("PUNCT", ")")
            return Sizeof(line=tok.line, type=type_tok.value + "*" * depth)
        operand = self._parse_unary()
        return Sizeof(line=tok.line, expr=operand)

    def _parse_cast(self) -> Cast:
        paren = self._expect("PUNCT", "(")
        type_tok = self._parse_base_type()
        depth = self._consume_stars()
        self._expect("PUNCT", ")")
        operand = self._parse_unary()
        return Cast(line=paren.line, type=type_tok.value + "*" * depth, operand=operand)

    def _parse_postfix(self):
        expr = self._parse_primary()
        while True:
            if self._check("OP") and self._peek().value in ("++", "--"):
                op_tok = self._advance()
                expr = UnaryOp(line=op_tok.line, op=op_tok.value, operand=expr, postfix=True)
            elif self._check("PUNCT", "["):
                bracket = self._advance()
                index = self._parse_expr()
                self._expect("PUNCT", "]")
                if isinstance(expr, ArrayAccess):
                    expr.indices.append(index)
                else:
                    # any other postfix expression can be the base of an
                    # index: a plain identifier (`a[i]`), a struct/union
                    # member that's itself an array (`b.vals[i]`,
                    # `p->vals[i]`), or an array-of-structs element
                    # that's itself indexed again (`boxes[i].vals[j]`).
                    expr = ArrayAccess(line=bracket.line, base=expr, indices=[index])
            elif self._check("PUNCT", ".") or self._check("OP", "->"):
                op_tok = self._advance()
                member_tok = self._expect("IDENT")
                expr = MemberAccess(line=op_tok.line, base=expr, member=member_tok.value,
                                     arrow=(op_tok.value == "->"))
            else:
                break
        return expr

    def _parse_primary(self):
        tok = self._peek()

        if tok.kind == "INT_CONST":
            self._advance()
            return Literal(line=tok.line, type="int", value=int(tok.value))

        if tok.kind == "FLOAT_CONST":
            self._advance()
            return Literal(line=tok.line, type="float", value=float(tok.value))

        if tok.kind == "CHAR_CONST":
            self._advance()
            return Literal(line=tok.line, type="char", value=tok.value)

        if tok.kind == "STRING_CONST":
            self._advance()
            return Literal(line=tok.line, type="string", value=tok.value)

        if tok.kind == "IDENT" and tok.value == "NULL":
            self._advance()
            return Literal(line=tok.line, type="null", value="NULL")

        if tok.kind == "IDENT":
            self._advance()
            if self._check("PUNCT", "("):
                self._advance()
                args = []
                if not self._check("PUNCT", ")"):
                    args.append(self._parse_expr())
                    while self._match("PUNCT", ","):
                        args.append(self._parse_expr())
                self._expect("PUNCT", ")")
                return Call(line=tok.line, name=tok.value, args=args)
            return Identifier(line=tok.line, name=tok.value)

        if tok.kind == "PUNCT" and tok.value == "(":
            self._advance()
            expr = self._parse_expr()
            self._expect("PUNCT", ")")
            return expr

        if tok.kind == "PUNCT" and tok.value == "{":
            raise ParseError("a brace-enclosed initialiser list is only allowed in a "
                             "declaration (e.g. 'int a[3] = {1, 2, 3};')", tok.line, tok.col)
        raise ParseError(f"expected an expression but found {_describe(tok)}", tok.line, tok.col)
