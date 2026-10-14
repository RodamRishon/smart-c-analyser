# Smart C Code Analyser and Optimiser

A command-line tool (plus a browser UI) that implements the front-end
and middle-end phases of a compiler for a practical subset of C:
preprocessing, lexical analysis, syntax analysis, semantic analysis
with a symbol table, three-address intermediate code generation, and
optimisation passes (constant folding, algebraic simplification,
common subexpression elimination, copy propagation, dead code
elimination).

Built for the BCSE307P Compiler Design Lab project, "Smart C Code
Analyser and Optimiser."

## Why a subset of C

Handling every corner of real C (multi-file translation units, the
full standard library) is a much bigger job than a lab project needs,
and would bury the compiler-design concepts the project is actually
about under parsing edge cases. The subset covers:

- types `int`, `float`, `char`, `double`, `void`
- variable declarations, including arrays of any dimension
  (`int grid[2][3]`), with optional initialisers
- `struct`/`union` declarations (including nested structs and
  self-referential ones via a pointer field, e.g. a linked-list node),
  member access with `.` and `->`, including a struct/union field
  that's itself an array (`b.vals[i]`, `p->vals[i]`) and arrays of
  structs whose elements have array fields (`boxes[i].vals[j]`)
- pointers of any depth (`int *p`, `char **argv`, `struct Node *next`),
  `&`/`*` (address-of/dereference), pointer arithmetic, explicit casts
  (`(int*) x`), and `sizeof`
- `malloc` / `calloc` / `realloc` / `free`, and basic stdio
  (`printf`, `scanf`, `puts`, ...) and `strlen` -- recognised as
  built-in library functions, so no header actually has to declare
  them
- a real preprocessor: object- and function-like `#define` macros
  (with recursive, self-reference-safe expansion), `#undef`,
  conditional compilation (`#ifdef` / `#ifndef` / `#if` / `#elif` /
  `#else` / `#endif`, with a real integer constant-expression
  evaluator including `defined(...)`), and `#include "local_file"`
- functions with parameters, return values, and calls
- `if` / `else`, `while`, `for`, `break`, `continue`, `return`
- the usual arithmetic, relational, logical, and assignment operators,
  plus `++` / `--` (prefix and postfix)
- `//` and `/* */` comments

That's enough to exercise every phase of the pipeline end to end --
including the aliasing problem pointers (and now struct members)
introduce for an optimiser (see "How each phase works" below).

## Project layout

```
smartc/
  lexer.py         Lexical analysis -> list of tokens
  ast_nodes.py     AST node definitions
  preprocessor.py  Macro expansion + conditional compilation + local #include
  parser.py        Recursive-descent parser -> AST
  semantic.py      Symbol table + semantic checks
  ir.py            AST -> three-address code (IR)
  optimizer.py     Type-aware constant folding/propagation (data-flow), algebraic
                   simplification, CSE, copy propagation, dead code elimination
  emitter.py       Optimised IR -> statements (temporaries folded back into expressions)
  structurer.py    Rebuilds if/else/while/for/break/continue from the IR's labels and
                   jumps at emission time; falls back to goto form when it cannot prove
                   an exact re-spelling
  cli.py           Ties the phases together and prints the plain-text report
  pipeline.py      Same pipeline, returns a structured dict (used by webapp.py)
analyse.py       CLI entry point
webapp.py        Flask web app entry point
templates/       Web app HTML
static/          Web app CSS/JS
tests/
  test_smartc.py           unit + differential test suite
  fuzz_differential.py     random-program differential tester (original vs optimised)
  programs/*.c             programs compiled and run before/after optimising
  proposal_example1.c      proposal Example 1 (semantic errors)
  proposal_example2.c      proposal Example 2 (optimisation)
  sample_valid.c           optimisable, error-free program
  sample_errors.c          semantic errors (undeclared var, type mismatch,
                            redeclaration, arg-count mismatch, scope error)
  sample_syntax_error.c    a missing semicolon
  sample_lexical_error.c   an invalid character + a malformed number
```

## Running it

No dependencies beyond the Python 3 standard library for the CLI.

```bash
python3 analyse.py tests/sample_valid.c
python3 analyse.py tests/sample_errors.c
python3 analyse.py tests/sample_syntax_error.c
python3 analyse.py tests/sample_lexical_error.c
```

or, run on your own file:

```bash
python3 analyse.py path/to/your_program.c
python3 analyse.py prog.c -o prog_opt.c    # also save the optimised C
python3 analyse.py prog.c --all-tokens     # print the full token stream
```

### Web app

A browser UI is also available, for pasting or uploading a `.c` file
and seeing every stage's output rendered live, without touching the
terminal. It requires Flask (`pip3 install -r requirements.txt`):

```bash
pip3 install -r requirements.txt
python3 webapp.py
```

Then open http://127.0.0.1:5000. The page lets you paste C source
(or upload a file), load the built-in sample, and click "Analyse &
Optimise" to see the token stream, parse result, semantic diagnostics
and symbol table, intermediate code before/after optimisation, the
optimisation log/stats, and the final optimised C -- the same report
`analyse.py` prints, as structured cards instead of plain text.

`smartc/pipeline.py` holds the shared `analyze(source) -> dict`
function the web app calls; `cli.py` still runs the original
plain-text report independently, so neither depends on the other.

Run the tests (the differential test needs `cc` or `gcc` on the PATH):

```bash
python3 -m unittest discover -s tests -v
```

Each run prints:

1. **Preprocessing** — macro expansion and conditional compilation
   applied, or preprocessor errors (unterminated `#if`, an unknown
   directive, a malformed macro invocation, ...) with line numbers.
2. **Lexical analysis** — the token stream, or lexical errors with line
   and column numbers.
3. **Syntax analysis** — confirmation of a successful parse, or syntax
   errors with line/column numbers (the parser recovers after each
   error, so more than one is reported per run where possible).
4. **Semantic analysis** — undeclared-identifier, redeclaration, type
   mismatch, argument-count, and scope errors, plus unused-variable
   warnings, followed by the full symbol table (name, kind, type,
   scope level, declaring line).
5. **Intermediate code (before optimisation)** — the three-address
   code generated per function.
6. **Optimisation** — one line per transformation with its source line
   (e.g. `Constant Folding: line 3: '2 + 3' evaluated at compile time -> 5`),
   then totals: passes performed, constants folded, algebraic
   simplifications, subexpressions reused, dead stores eliminated,
   unreachable code removed, copies propagated, and instruction/line
   counts before vs. after.
7. **Intermediate code (after optimisation)** — the optimised
   three-address code.
8. **Optimised C code** — the result as compilable C, with any
   `struct`/`union` definitions it uses re-emitted up front. `if`/`else`,
   `while`, `for`, `break` and `continue` are written back as such (the IR
   keeps labels and jumps internally; they are not exposed). A function is
   written with `goto`/labels only if its structure cannot be rebuilt
   exactly, and the line "Control flow in the output" counts loops, `if`s
   and `goto`s directly from the emitted text.

A stage that finds errors stops the pipeline there, the same way a
real compiler won't generate code for a program that doesn't parse or
doesn't type-check.

## How each phase works

**Preprocessing** (`preprocessor.py`) runs before lexing, on the raw
source text: strips comments and joins backslash-continued lines,
expands object- and function-like `#define` macros (token-aware, with
argument substitution and a "currently expanding" guard so a
self-referential macro like `#define SELF (SELF + 1)` doesn't recurse
forever -- matching gcc's own `cpp` behaviour, which the test suite
checks directly), handles `#undef`, evaluates `#ifdef`/`#ifndef`/`#if`/
`#elif`/`#else`/`#endif` with a real recursive-descent integer
constant-expression evaluator (arithmetic, comparisons, `&&`/`||`,
`defined(...)`, parentheses), and splices in `#include "local_file"`
(system `#include <header.h>` is a no-op -- see "Known limitations").
Every directive line is blanked rather than removed, so line numbers
in every later diagnostic still point at the file the user wrote.

**Lexical analysis** (`lexer.py`) scans the source character by
character and emits tokens (keywords, identifiers, numeric/char/string
literals, operators, punctuation), reporting unrecognised characters,
unterminated strings/chars/comments, and malformed numbers as it goes,
without stopping at the first one.

**Syntax analysis** (`parser.py`) is a recursive-descent parser, one
function per grammar rule, built around standard operator-precedence
climbing for expressions. It accepts function definitions and
prototypes, nested blocks, `if`/`while`/`for` (including
`for (int i = 0; ...)`), pointer declarations and `&`/`*`, calls,
and array declarations with brace initialisers
(`int a[5] = {10, 20, 30, 40, 50};`, `int a[] = {1, 2, 3};`,
`int g[2][2] = {{1, 2}, {3, 4}};`, trailing comma allowed). Semantic
analysis lays the initialiser out row-major, checks element types and
rejects too many elements.

Error recovery is deliberately conservative so that **one mistake gives
one error**. After a syntax error the parser skips to the end of the
statement, but it counts braces as it goes (so the `}` that closes an
initialiser list is not mistaken for the end of the enclosing block) and
stops early at a statement-starting keyword (so a missing `;` does not
swallow the next line). A missing `}` at end of file is reported against
the line of the `{` that was never closed. Errors carry line and column
and name the token actually found.

**Semantic analysis** (`semantic.py`) walks the AST with a symbol
table implemented as a stack of scopes — one dictionary per block,
pushed on entry and popped on exit, with the global scope at the
bottom. It checks declaration-before-use, redeclaration, scope rules,
type compatibility on assignments and binary operations, and
function-call argument counts, and flags variables that are declared
but never read. Pointer types are just strings with the stars baked
in (`"int*"`, `"char**"`) so most of the analyser treats them like any
other type; `&`/`*` compute/strip one level of pointer-ness, `NULL`
and `void*` convert silently to any pointer type, and mismatched
concrete pointer types (e.g. `int*` into `char*`) are a warning rather
than an error, matching how permissive real C compilers are here.
`malloc`/`calloc`/`realloc`/`free` and a handful of stdio functions
are pre-registered as builtins (`BUILTIN_FUNCS`) so they resolve
without a declaration. Array types carry a dimension list
(`array_dims`) rather than just one size, and indexing an `int a[2][3]`
with fewer indices than it has dimensions (`a[0]` alone) is reported
as unsupported partial indexing rather than silently misread, since
modelling the resulting "row" type is out of scope here; indexing a
pointer twice (`int **pp; pp[0][0]`) is handled the same way, by
peeling one level of pointer-ness per index. `struct`/`union` tags are
their own namespace (`self.structs`, keyed by tag name, separate from
variables) so a struct and a variable can share a name; tags are
pre-registered in a first pass (like function signatures) so two
structs can point at each other, and a struct can contain a pointer to
itself. `.`/`->` member access is type-checked against the owning
struct's field table, with a clear error for an unknown tag, an
unknown member, or using `.` on a pointer (or `->` on a non-pointer)
instead of silently accepting it.

**IR generation** (`ir.py`) lowers the AST into three-address code —
one operation per instruction, using generated temporaries (`t0`,
`t1`, ...) for intermediate results, and explicit labels/gotos for
control flow. This is the representation the optimiser works on,
mirroring how a real compiler's middle end sits between the front end
and code generation. Because the IR is a flat instruction list with
no block structure of its own, a variable that shadows an outer one
(`int x = 1; { int x = 2; ... } `) is alpha-renamed to a unique IR
name (`x`, `x__2`, ...) via a scope-aware resolver that mirrors the
AST's block nesting — otherwise the inner declaration would silently
overwrite the outer variable's value once both landed in the same
flat list. `struct`/`union` tag declarations produce no instructions
at all (they're a pure type-system fact, handled entirely in semantic
analysis and re-emitted straight from the AST -- see `emit_struct_defs`
in `emitter.py`); member access just extends the same "operand is
really just a piece of text" approach already used for arrays and
pointers, lowering `s.x` / `p->x` to the literal text `"s.x"` /
`"p->x"`, including chains like `p->next->val` or `a[i].b`.

**Optimisation** (`optimizer.py`) runs these passes to a fixed point,
described in detail in the module's own docstring. The IR keeps operands
as text, so the passes share a small **type model** (`OptContext`:
declared types of variables, parameters, globals and function results,
which names are arrays, the type of every temporary, and which variables
can change behind the optimiser's back) and refuse any rewrite whose
correctness depends on a type they cannot establish.

- *Constant folding and propagation* use C's rules, not Python's:
  integer division truncates toward zero, `%` takes the sign of the
  dividend, and an expression is **not** folded when it would overflow
  `int` or divide by zero (those are reported instead). A constant
  assigned to a variable is first converted to that variable's type
  (`double d = 1;` records `1.0`, `int i = 2.7;` records `2`), and only
  `int`, `double` and small `char` values are tracked, so substituting
  the constant never changes the type of a later expression.
  Which variables hold a known constant at the start of each basic
  block is found with a forward data-flow analysis over the CFG
  (a value survives a join only if every incoming path agrees), so
  `int x = 5; if (c) {...} return x + 1;` folds to `return 6;`
  and a variable changed inside a loop is correctly *not* propagated.
  A branch whose condition becomes constant is resolved, exposing
  dead code to the next pass.
- *Algebraic simplification* applies an identity only if it is exact
  for the operand type: `x + 0`, `0 + x`, `x * 0`, `x - x` for
  integers; `x - 0`, `x * 1`, `x / 1` for any number. `x * 0` and
  `x + 0` are **not** applied to floating point (they change `-0.0`
  and NaN results); they are listed under "detected but not applied".
  The replacement must also have the same type as the original
  expression (`i * 1.0` is a `double`).
- *Common subexpression elimination*, per basic block, including
  commutative matches. A result is reused only if the variable holding
  it has the expression's type and does not appear in the expression
  itself (`c += d` is not a computation of `c + d`). Entries are
  invalidated by writes to operands, by stores that may alias memory
  they read through a pointer, and by calls.
- *Copy propagation* replaces a copy by its source only when both have
  the same declared type (`double d = i; ... d / 4` must stay a double
  division).
- *Dead code elimination*: dead stores (backward liveness over the CFG),
  unreachable code, and variables nothing uses. A variable whose
  address is taken, and every global, is never treated as dead, because
  it can be read through a pointer or by a callee.
- *Short-circuit evaluation is preserved.* `a && b` / `a || b` are
  lowered to branches whenever `b` could fault or have an effect
  (`p != NULL && *p > 0`, `i < n && a[i]`, a call), so the right side
  is still skipped. A cheap, side-effect-free right side (`a < b && b < c`)
  stays straight-line.
- *Single-use variable elimination.* A local that is assigned once and
  read once, in the same basic block, with nothing in between that could
  change the value (`int result = total * 2; return result;`) is replaced
  by its value at the point of use (`return total * 2;`) and its
  declaration disappears. It is skipped for variables read twice, whose
  address escapes, or whose value would have to move past a write to
  something it reads.
- *Redundant self-assignment removal.* `x = x`, `a[2] = a[2]`,
  `s.f = s.f`, and a value loaded from a location and stored straight
  back (`t = *p; *p = t;`) are removed **only when the location is proven
  valid** (a pointer known to point at a live local, an array index that
  is a constant in range). `*p = *p` for a pointer parameter, or
  `a[k] = a[k]` with an unknown `k`, could be a crash in the original, so
  it is kept and listed under "detected but not applied".
- *Dead code never drops an effect.* A dead assignment from a call still
  makes the call; a dead `a / b` or `*p` that could trap is kept (emitted
  as `__auto_type t = a / b; (void)t;`, because a bare `(void)(a / b);`
  is simply discarded by the C compiler) and reported as detected, not
  removed.
- The emitter writes `int result = x + y;` rather than a separate
  declaration and assignment, and inlines single-use temporaries. A
  temporary that reads memory (`*p`, `a[i]`, `p->f`) is written out
  before any later write to a named variable, since that write may be
  through an alias.
- *Loop structure.* `while` and `for` loops are rebuilt by `structurer.py`
  from the label names the IR generator uses (`L_whileN`, `L_forN`,
  `L_forcontN`, `L_endwhileN`, ...). `for (init; cond; step)` is used when
  the statement before the loop initialises something the header uses and
  the variable is not used after the loop; the update goes in the header
  when it is plain expressions, otherwise the loop is written as `while`
  (or left as labels if a `continue` would skip an update that does not
  fit in the header). Before accepting a rewrite the structurer checks
  that no jump is left pointing at an absorbed label and that nothing
  declared inside new braces is used after them; otherwise the function
  stays in `goto` form. The IR printed by the tool is unchanged.

The report has three parts and never over-claims: **Applied** (changes
made), **Detected but not applied** (opportunities found and left alone
for safety, with the reason), and **Not supported** (techniques this tool
does not attempt: loop-invariant code motion, strength reduction, inlining,
global CSE, ...).

The pass loop stops when a whole round changes nothing; a cap of 30
rounds guards against passes undoing each other, and the report says so
if the cap is ever hit.

Pointers (and now struct/union member writes through them, e.g.
`p->x = v`) mean a store or a call can affect memory the optimiser
can't see by name, so each pass has a conservative fallback for that:
a write through a dereference or a member access (`*p = v`, `s.f = v`,
`p->f = v`, `a[i].f = v`, ...) drops every cached CSE entry and
copy-propagation alias instead of trying to figure out what it may
have aliased (`_is_unsafe_write` in `optimizer.py` is the single check
every pass uses for this), and a call drops anything whose address was
ever taken with `&` (tracked per-pass via `_address_taken_vars`) in
addition to globals. A plain array-element store (`a[i] = v`,
including multi-dimensional `a[i][j] = v`) still gets the narrower,
more precise per-name invalidation it always did -- only pointer/
member writes take the fully conservative path. That costs some
optimisation opportunities across those points but keeps the output
correct — see `test_no_bogus_fold_across_aliasing_call` /
`test_pointer_store_not_dead` / `test_struct_pointer_write_not_folded_across_call`
in `tests/test_smartc.py` for the cases this guards against (a value
changed through a pointer or a struct member getting folded as if it
were still its old compile-time-known value).

Rather than hand-write a parser for every new composite operand form
(multi-dim indices, member access, dereference, sizeof, casts, and
every combination of them), `_extract_uses` in `optimizer.py` uses one
general fallback for anything that isn't a bare name or a literal:
collect every identifier-looking token the text contains. That's
always *safe* (over-including a "use" can only block an optimisation,
never cause something still-live to be treated as dead) without
needing bespoke structural parsing for each new nested form.

CSE and copy propagation work on basic blocks (a new block starts at a
label or right after a branch/return); constant propagation and
dead-store elimination use whole-function data-flow.

## Testing

```bash
python3 -m unittest discover -s tests          # 159 tests, ~25 s
python3 tests/fuzz_differential.py 400 7       # 400 random programs, seed 7
```

The central check is **differential**: every program in `tests/programs/`
is compiled with the system C compiler twice, as written and as emitted
by the optimiser, and the stdout and exit status must be identical.
`fuzz_differential.py` does the same for randomly generated programs
(int/double/char mixing, shadowing, loops, pointers, arrays, globals,
side-effecting calls inside `&&`/`||`); it compiles the original with
UBSan and skips any program that is itself undefined, so a reported
difference is an optimiser bug. It found two real bugs during
development (a store to an address-taken variable removed as dead, and a
temporary passed twice to one call being inlined once), both now covered
by regression programs. Known-bad mutations of the optimiser (floor
division, eager `&&`, unsafe float identities, unconverted constants,
ignoring address-taken variables) are all caught by it.

## Known limitations

This is a teaching-scale tool, not a production compiler:

- **Preprocessor**: `#define` (object- and function-like, with
  recursive expansion), `#undef`, `#ifdef`/`#ifndef`/`#if`/`#elif`/
  `#else`/`#endif` (with a real expression evaluator), and
  `#include "local_file"` are implemented and tested against gcc's own
  preprocessor output (see `PreprocessorDifferential` in
  `tests/test_smartc.py`). What's *not* there: stringification (`#x`),
  token-pasting (`##`), variadic macros (`...`/`__VA_ARGS__`), and
  system `#include <header.h>` (a no-op -- there's no bundled standard
  library to provide; the functions those headers would declare are
  already known as builtins instead, see above). An included file's
  line numbers aren't separately tracked, so diagnostics after an
  `#include` use the combined line count rather than restarting per
  file.
- **Structs/unions**: declarations, nested structs, self-referential
  structs via a pointer field, and `.`/`->` member access (including
  chains) are implemented and type-checked. Not supported: enums,
  anonymous structs/unions, bit-fields, and the combined
  `struct Name { ... } var;` declaration form (declare the tag and the
  variable as two separate statements instead). Struct/union tags also
  share one flat namespace in this tool regardless of their actual C
  block scope -- two *different* local structs in two different
  functions can't reuse the same tag name, where real C would allow it.
- **Multi-dimensional arrays**: declaration and indexing of any number
  of dimensions (`int cube[2][2][2]`) are implemented, with row-major
  layout handled by the real C compiler that builds the output (the
  emitted array declarations use genuine multi-dimensional C syntax,
  so this tool never needs to compute offsets itself). Partial
  indexing (`a[0]` on a 2-D array, meaning "give me row 0") is
  rejected with a clear error rather than silently misread, since
  modelling the resulting row type is out of scope here.
- No general alias/points-to analysis -- see the optimiser note above.
  Pointer writes, struct/union member writes, and calls that might
  alias through a pointer fall back to conservative (correct, but less
  aggressive) behaviour rather than tracking exactly what each pointer
  could point to. This is sound (it never assumes two pointers are
  distinct without evidence) but not precise (it can miss real
  optimisation opportunities around those points).
- Constant propagation is whole-function (data-flow over the CFG), but
  **CSE and copy propagation are still block-local**: a value computed
  in one basic block is not reused in another even where it provably
  still holds (that needs available-expressions analysis / global value
  numbering, not attempted here).
- Types the parser accepts are `int`, `char`, `float`, `double`, `void`,
  pointers, `struct`/`union` and arrays. Not supported: `short`, `long`,
  `unsigned`/`signed`, `const`, `enum`, float literal suffixes (`1.5f` is a
  lexical error), designated initialisers (`{[1] = 5}`, rejected with an
  explicit message), array parameters (`int a[]` -- use `int *a`), and
  variadic function *declarations*. Only `int`, `double` and small `char`
  values are tracked as compile-time constants; `float` is never
  substituted, to keep its rounding exact.
- No enums or bitwise shift operators in `#if` (`<<`/`>>`). `#if`
  expressions are evaluated with Python integers. The optimiser, by
  contrast, models 32-bit `int`: a constant expression whose result
  would overflow (or whose operand is outside `int`) is left for the C
  compiler and reported as "detected but not applied". Floating-point
  folding uses IEEE doubles (the same as the C target) and is skipped
  if the result is not finite. Signed-overflow and division-by-zero are
  undefined in C; the optimiser nevertheless keeps a *dead* operation
  that would have trapped, and reports it, rather than relying on that.
- Pointer arithmetic (`p + 1`, `p - 1`) is passed straight through to
  the emitted C, which is where it actually gets scaled by the
  pointee's size (via the real C compiler that builds the output) --
  this tool's own passes never need to compute that scaling.
- Structured output covers the loops and branches this language can
  express (`if`/`else`, `while`, `for`, `break`, `continue`; there is no
  `do`/`while`, `switch` or `goto` in the accepted subset). The result of a
  lowered `&&` / `||` appears as nested `if`s assigning a temporary. A
  `while` whose condition needs a statement first (a call result, a
  lowered `&&`) is written `while (1) { ...; if (!c) { break; } ... }`.
- Optimised C uses `__auto_type` (GCC/Clang) for the rare temporary
  that cannot be folded back into an expression (including the result
  of a lowered `&&` / `||`).
- No actual machine-code generation — the "optimised code" produced
  is the optimised three-address IR, which is what the project
  proposal asks for.

These are deliberate scoping decisions, not oversights, and are good
talking points if asked about possible extensions.

## References

Same as the project proposal: Aho/Lam/Sethi/Ullman's *Compilers:
Principles, Techniques, and Tools*; Levine's *Flex & Bison*; Louden's
*Compiler Construction*; Kernighan & Ritchie's *The C Programming
Language*.
