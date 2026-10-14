"""
Lexical Analyser
-----------------
Scans C source code character by character and produces a stream of
tokens. Any character sequence that cannot form a valid token (an
unknown symbol, an unterminated string/char literal, an unterminated
block comment, a malformed number) is reported as a lexical error with
its line number, but scanning continues so later errors in the same
file are still found.

This lexer covers a practical subset of C that is enough to exercise
every phase of a compiler: the basic scalar types, control flow,
functions, arrays (including multi-dimensional), pointers, structs
and unions (`struct`/`union`, `.`/`->`), and the common operators.
Preprocessing (`#define`, `#include`, conditional compilation) is a
separate pass that runs over the raw source text before this lexer
ever sees it -- see preprocessor.py.
"""

from dataclasses import dataclass
from typing import List, Optional


KEYWORDS = {
    "int", "float", "char", "double", "void",
    "if", "else", "while", "for", "return",
    "break", "continue", "sizeof", "struct", "union",
}

# Multi-character operators must be matched before their single-char
# prefixes (e.g. "==" before "=").
MULTI_CHAR_OPS = [
    "==", "!=", "<=", ">=", "&&", "||", "++", "--",
    "+=", "-=", "*=", "/=", "%=", "<<", ">>", "->",
]

SINGLE_CHAR_OPS = set("+-*/%=<>!&|^~")
PUNCTUATION = set("(){}[];,.")


@dataclass
class Token:
    kind: str       # KEYWORD, IDENT, INT_CONST, FLOAT_CONST, CHAR_CONST,
                     # STRING_CONST, OP, PUNCT, EOF
    value: str
    line: int
    col: int

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, line={self.line})"


@dataclass
class LexError:
    message: str
    line: int
    col: int

    def __str__(self):
        col_suffix = f" [col {self.col}]" if self.col else ""
        return f"Lexical Error (line {self.line}){col_suffix}: {self.message}"


class Lexer:
    def __init__(self, source: str):
        self.src = source
        self.pos = 0
        self.line = 1
        self.col = 1
        self.n = len(source)
        self.tokens: List[Token] = []
        self.errors: List[LexError] = []

    # -- low level helpers -------------------------------------------------
    def _peek(self, offset: int = 0) -> str:
        i = self.pos + offset
        return self.src[i] if i < self.n else ""

    def _advance(self) -> str:
        ch = self.src[self.pos]
        self.pos += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _at_end(self) -> bool:
        return self.pos >= self.n

    # -- main entry point ----------------------------------------------
    def tokenize(self) -> List[Token]:
        while not self._at_end():
            ch = self._peek()

            if ch in " \t\r\n":
                self._advance()
                continue

            if ch == "/" and self._peek(1) == "/":
                self._skip_line_comment()
                continue

            if ch == "/" and self._peek(1) == "*":
                self._skip_block_comment()
                continue

            if ch.isdigit() or (ch == "." and self._peek(1).isdigit()):
                self._read_number()
                continue

            if ch.isalpha() or ch == "_":
                self._read_identifier()
                continue

            if ch == '"':
                self._read_string()
                continue

            if ch == "'":
                self._read_char()
                continue

            if self._try_multi_char_op():
                continue

            if ch in SINGLE_CHAR_OPS:
                line, col = self.line, self.col
                self._advance()
                self.tokens.append(Token("OP", ch, line, col))
                continue

            if ch in PUNCTUATION:
                line, col = self.line, self.col
                self._advance()
                self.tokens.append(Token("PUNCT", ch, line, col))
                continue

            # unrecognised character
            line, col = self.line, self.col
            self.errors.append(
                LexError(f"unrecognised character '{ch}'", line, col)
            )
            self._advance()

        self.tokens.append(Token("EOF", "", self.line, self.col))
        return self.tokens

    # -- token readers ---------------------------------------------------
    def _skip_line_comment(self):
        while not self._at_end() and self._peek() != "\n":
            self._advance()

    def _skip_block_comment(self):
        start_line = self.line
        self._advance()  # '/'
        self._advance()  # '*'
        while True:
            if self._at_end():
                self.errors.append(
                    LexError("unterminated block comment", start_line, 1)
                )
                return
            if self._peek() == "*" and self._peek(1) == "/":
                self._advance()
                self._advance()
                return
            self._advance()

    def _read_number(self):
        line, col = self.line, self.col
        start = self.pos
        is_float = False

        while self._peek().isdigit():
            self._advance()

        if self._peek() == "." and self._peek(1).isdigit():
            is_float = True
            self._advance()
            while self._peek().isdigit():
                self._advance()

        # exponent part, e.g. 1e10, 2.5e-3
        if self._peek() in ("e", "E"):
            lookahead = 1
            if self._peek(lookahead) in ("+", "-"):
                lookahead += 1
            if self._peek(lookahead).isdigit():
                is_float = True
                self._advance()
                if self._peek() in ("+", "-"):
                    self._advance()
                while self._peek().isdigit():
                    self._advance()

        text = self.src[start:self.pos]

        # catch malformed numbers like "12abc"
        if self._peek().isalpha() or self._peek() == "_":
            bad_start = self.pos
            while self._peek().isalnum() or self._peek() == "_":
                self._advance()
            bad_text = text + self.src[bad_start:self.pos]
            self.errors.append(
                LexError(f"malformed numeric literal '{bad_text}'", line, col)
            )
            return

        kind = "FLOAT_CONST" if is_float else "INT_CONST"
        self.tokens.append(Token(kind, text, line, col))

    def _read_identifier(self):
        line, col = self.line, self.col
        start = self.pos
        while self._peek().isalnum() or self._peek() == "_":
            self._advance()
        text = self.src[start:self.pos]
        kind = "KEYWORD" if text in KEYWORDS else "IDENT"
        self.tokens.append(Token(kind, text, line, col))

    def _read_string(self):
        line, col = self.line, self.col
        self._advance()  # opening quote
        start = self.pos
        while True:
            if self._at_end() or self._peek() == "\n":
                self.errors.append(
                    LexError("unterminated string literal", line, col)
                )
                return
            if self._peek() == "\\":
                self._advance()
                if not self._at_end():
                    self._advance()
                continue
            if self._peek() == '"':
                text = self.src[start:self.pos]
                self._advance()  # closing quote
                self.tokens.append(Token("STRING_CONST", text, line, col))
                return
            self._advance()

    def _read_char(self):
        line, col = self.line, self.col
        self._advance()  # opening quote
        start = self.pos
        if self._peek() == "\\":
            self._advance()
            if not self._at_end():
                self._advance()
        elif self._peek() != "'":
            self._advance()

        if self._peek() != "'":
            self.errors.append(
                LexError("unterminated or malformed char literal", line, col)
            )
            # best-effort recovery: skip to next quote or newline
            while not self._at_end() and self._peek() not in ("'", "\n"):
                self._advance()
            if self._peek() == "'":
                self._advance()
            return

        text = self.src[start:self.pos]
        self._advance()  # closing quote
        self.tokens.append(Token("CHAR_CONST", text, line, col))

    def _try_multi_char_op(self) -> bool:
        two = self._peek() + self._peek(1)
        if two in MULTI_CHAR_OPS:
            line, col = self.line, self.col
            self._advance()
            self._advance()
            self.tokens.append(Token("OP", two, line, col))
            return True
        return False
