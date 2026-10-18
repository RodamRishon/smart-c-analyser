"""
Test suite. Run from the project root:   python3 -m unittest discover -s tests -v

The key test is differential: every program in tests/programs is
compiled and run twice with the system C compiler -- once as written,
once as the optimiser's emitted C -- and the exit codes must match.
That checks the optimiser preserves behaviour, not just that it
produces something plausible-looking.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from glob import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from smartc.cli import run                      # noqa: E402
from smartc.lexer import Lexer                  # noqa: E402
from smartc.parser import Parser                # noqa: E402
from smartc.semantic import SemanticAnalyzer    # noqa: E402
from smartc.preprocessor import strip_directives  # noqa: E402

CC = shutil.which("cc") or shutil.which("gcc")


def analyse(path, out=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = run(path, out_path=out)
    return code, buf.getvalue()


def semantic(src):
    toks = Lexer(src).tokenize()
    program = Parser(toks).parse()
    a = SemanticAnalyzer()
    return [str(d) for d in a.analyze(program)], a


class FrontEnd(unittest.TestCase):
    def test_lexical_errors_reported(self):
        code, out = analyse(os.path.join(ROOT, "tests/sample_lexical_error.c"))
        self.assertEqual(code, 1)
        self.assertIn("unrecognised character '@'", out)
        self.assertIn("malformed numeric literal '12abc'", out)

    def test_syntax_error_has_line(self):
        code, out = analyse(os.path.join(ROOT, "tests/sample_syntax_error.c"))
        self.assertEqual(code, 1)
        self.assertIn("Syntax Error (line 8)", out)

    def test_proposal_example1(self):
        code, out = analyse(os.path.join(ROOT, "tests/proposal_example1.c"))
        self.assertEqual(code, 1)
        self.assertIn("Semantic Error (line 6): use of undeclared identifier 'd'", out)
        self.assertIn("Semantic Error (line 7): type mismatch in e: cannot assign 'string' to 'float'", out)

    def test_multi_declarator_shares_scope(self):
        diags, a = semantic("int main(){ int x, y; x = 1; y = x; return y; }")
        self.assertFalse([d for d in diags if "Error" in d], diags)
        self.assertTrue(all(s.scope_level == 1 for s in a.all_symbols if s.name in "xy"))

    def test_scope_error(self):
        diags, _ = semantic("int main(){ { int k = 1; } return k; }")
        self.assertTrue(any("'k'" in d and "Error" in d for d in diags), diags)

    def test_redeclaration(self):
        diags, _ = semantic("int main(){ int a; int a; return 0; }")
        self.assertTrue(any("Error" in d for d in diags), diags)

    def test_pointer_deref_and_address_of(self):
        diags, a = semantic(
            "int main(){ int x = 1; int *p = &x; *p = 2; return *p; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)
        p = next(s for s in a.all_symbols if s.name == "p")
        self.assertEqual(p.type, "int*")

    def test_dereference_non_pointer_is_error(self):
        diags, _ = semantic("int main(){ int x = 1; *x = 2; return 0; }")
        self.assertTrue(any("dereference" in d for d in diags), diags)

    def test_malloc_free_recognised_without_declaration(self):
        diags, _ = semantic(
            "int main(){ int *p = malloc(4); free(p); return 0; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_2d_array_declared_and_indexed(self):
        diags, a = semantic(
            "int main(){ int grid[2][3]; grid[0][1] = 5; return grid[0][1]; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)
        grid = next(s for s in a.all_symbols if s.name == "grid")
        self.assertEqual(grid.array_dims, [2, 3])

    def test_partial_array_indexing_rejected(self):
        diags, _ = semantic(
            "int main(){ int a[2][3]; int row; row = a[0]; return 0; }"
        )
        self.assertTrue(any("partial array indexing" in d for d in diags), diags)

    def test_too_many_array_indices_rejected(self):
        diags, _ = semantic(
            "int main(){ int a[2][3]; int x; x = a[0][1][2]; return 0; }"
        )
        self.assertTrue(any("too many indices" in d for d in diags), diags)

    def test_double_pointer_double_indexing(self):
        diags, _ = semantic(
            "int main(){ int x = 1; int *p = &x; int **pp = &p; "
            "return pp[0][0]; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_struct_decl_and_member_access(self):
        diags, a = semantic(
            "struct Point { int x; int y; }; "
            "int main(){ struct Point p; p.x = 3; p.y = 4; return p.x + p.y; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)
        p = next(s for s in a.all_symbols if s.name == "p")
        self.assertEqual(p.type, "struct Point")
        self.assertIn("Point", a.structs)
        self.assertEqual(a.structs["Point"].fields, {"x": "int", "y": "int"})

    def test_struct_pointer_arrow_access(self):
        diags, _ = semantic(
            "struct Point { int x; }; "
            "int main(){ struct Point p; struct Point *pp = &p; "
            "pp->x = 5; return pp->x; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_nested_struct_member_access(self):
        diags, _ = semantic(
            "struct Point { int x; }; struct Line { struct Point start; }; "
            "int main(){ struct Line l; l.start.x = 1; return l.start.x; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_self_referential_struct(self):
        diags, _ = semantic(
            "struct Node { int val; struct Node *next; }; "
            "int main(){ struct Node n; n.next = 0; return n.val; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_undeclared_struct_tag_is_error(self):
        diags, _ = semantic("int main(){ struct Unknown u; return 0; }")
        self.assertTrue(any("undeclared struct" in d for d in diags), diags)

    def test_unknown_struct_member_is_error(self):
        diags, _ = semantic(
            "struct Point { int x; int y; }; "
            "int main(){ struct Point p; return p.z; }"
        )
        self.assertTrue(any("no member 'z'" in d for d in diags), diags)

    def test_dot_on_pointer_is_error(self):
        diags, _ = semantic(
            "struct Point { int x; }; "
            "int main(){ struct Point p; struct Point *pp = &p; return pp.x; }"
        )
        self.assertTrue(any("requires a struct/union value" in d for d in diags), diags)

    def test_arrow_on_non_pointer_is_error(self):
        diags, _ = semantic(
            "struct Point { int x; }; "
            "int main(){ struct Point p; return p->x; }"
        )
        self.assertTrue(any("requires a pointer to a struct/union" in d for d in diags), diags)

    def test_union_decl_and_member_access(self):
        diags, a = semantic(
            "union Value { int i; float f; }; "
            "int main(){ union Value v; v.i = 1; return v.i; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)
        self.assertTrue(a.structs["Value"].is_union)

    def test_struct_redefinition_is_error(self):
        diags, _ = semantic(
            "struct Point { int x; }; struct Point { int y; }; "
            "int main(){ return 0; }"
        )
        self.assertTrue(any("redefinition" in d for d in diags), diags)

    # -- struct fields that are themselves arrays, and arrays of
    # structs with array fields -- a mixed member/index chain --------
    def test_struct_array_field_indexed_via_dot(self):
        diags, a = semantic(
            "struct Box { int vals[3]; }; "
            "int main(){ struct Box b; b.vals[0] = 1; return b.vals[0]; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_struct_array_field_indexed_via_arrow(self):
        diags, _ = semantic(
            "struct Box { int vals[3]; }; "
            "int main(){ struct Box b; struct Box *p = &b; "
            "p->vals[0] = 1; return p->vals[0]; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_array_of_structs_with_array_field(self):
        diags, _ = semantic(
            "struct Box { int vals[2]; }; "
            "struct Boxes { struct Box items[2]; }; "
            "int main(){ struct Boxes bs; "
            "bs.items[0].vals[1] = 7; return bs.items[0].vals[1]; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_indexing_non_array_struct_field_is_error(self):
        diags, _ = semantic(
            "struct Point { int x; }; "
            "int main(){ struct Point p; return p.x[0]; }"
        )
        self.assertTrue(any("is not an array or pointer" in d for d in diags), diags)

    def test_address_of_struct_member_allowed(self):
        diags, _ = semantic(
            "struct Point { int x; }; "
            "void inc(int *p) { *p = *p + 1; } "
            "int main(){ struct Point p; p.x = 1; inc(&p.x); return p.x; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)

    def test_increment_struct_member_allowed(self):
        diags, _ = semantic(
            "struct Point { int x; }; "
            "int main(){ struct Point p; p.x = 1; p.x++; ++p.x; return p.x; }"
        )
        self.assertFalse([d for d in diags if "Error" in d], diags)


class Optimiser(unittest.TestCase):
    def emitted(self, src):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.c")
            with open(p, "w") as fh:
                fh.write(src)
            code, out = analyse(p)
        self.assertEqual(code, 0, out)
        return out

    def c_section(self, out):
        return out.split("OPTIMISED C CODE")[1]

    def test_constant_folding_and_dead_branch(self):
        out = self.emitted("int main(){ int x = 2 + 3; if (0) { x = 9; } return x * 4; }")
        self.assertIn("'2 + 3' evaluated at compile time -> 5", out)
        self.assertIn("always false", out)
        self.assertIn("return 20;", self.c_section(out))

    def test_cse_commutative(self):
        out = self.emitted("int f(int a,int b){ int s = a+b; int t = b+a; return s*t; }"
                           "int main(){ return f(1,2); }")
        self.assertIn("already computed at line", out)
        self.assertIn("s * s", self.c_section(out))

    def test_global_store_not_dead(self):
        out = self.emitted("int g; void s(){ g = 42; } int main(){ s(); return g; }")
        self.assertIn("g = 42;", self.c_section(out))

    def test_array_store_not_dead(self):
        out = self.emitted("int main(){ int a[2]; int i = 1; a[i] = 7; return a[1]; }")
        self.assertIn("] = 7;", self.c_section(out))

    def test_2d_array_store_not_dead(self):
        out = self.emitted(
            "int main(){ int g[2][2]; int i = 1; g[i][0] = 7; return g[1][0]; }"
        )
        self.assertIn("] = 7;", self.c_section(out))

    def test_unreachable_after_return(self):
        out = self.emitted("int main(){ return 1; int z = 5; z = z + 1; }")
        self.assertIn("unreachable", out)

    def test_pointer_store_not_dead(self):
        out = self.emitted(
            "void set(int *p){ *p = 42; } "
            "int main(){ int x = 0; set(&x); return x; }"
        )
        self.assertIn("*p = 42;", self.c_section(out))

    def test_no_bogus_fold_across_aliasing_call(self):
        # x's value changes inside swap() through a pointer -- the
        # optimiser must not treat `x` as still being its old constant
        # value afterwards.
        out = self.emitted(
            "void swap(int *a, int *b){ int t = *a; *a = *b; *b = t; } "
            "int main(){ int x = 3; int y = 7; swap(&x, &y); return x + y; }"
        )
        self.assertIn("return x + y;", self.c_section(out))

    def test_struct_member_store_not_dead(self):
        out = self.emitted(
            "struct Point { int x; }; "
            "int main(){ struct Point p; p.x = 42; return p.x; }"
        )
        self.assertIn("p.x = 42;", self.c_section(out))

    def test_struct_pointer_write_not_folded_across_call(self):
        # set() writes through a struct pointer -- the optimiser must
        # not assume p.x is still its old value afterwards.
        out = self.emitted(
            "struct Point { int x; }; "
            "void set(struct Point *q){ q->x = 42; } "
            "int main(){ struct Point p; p.x = 0; set(&p); return p.x; }"
        )
        self.assertIn("return p.x;", self.c_section(out))

    # -- redundant self-assignment ('c = c') ------------------------------
    def test_literal_self_assignment_removed(self):
        # x isn't a compile-time constant (it's a parameter), so this
        # can't be resolved by constant propagation alone -- the plain
        # `x = x;` has to be recognised as a no-op directly.
        out = self.emitted("int id(int x){ x = x; return x; }"
                           "int main(){ return id(5); }")
        c = self.c_section(out)
        self.assertNotIn("x = x;", c)
        self.assertIn("redundant self-assignment", out)

    def test_self_assignment_from_algebraic_simplification(self):
        # `x = x + 0` simplifies to `x = x` first, which must then also
        # be removed rather than left behind as a no-op statement.
        out = self.emitted("int bump(int x){ x = x + 0; return x; }"
                           "int main(){ return bump(5); }")
        c = self.c_section(out)
        self.assertNotIn("x = x;", c)

    def test_self_assignment_from_copy_propagation(self):
        # `w = v; v = w;` -- copy propagation substitutes w with v in
        # the second statement, producing `v = v;`, which must then
        # also be cleaned up rather than left in the output.
        out = self.emitted("int f(int v){ int w = v; v = w; return v; }"
                           "int main(){ return f(3); }")
        c = self.c_section(out)
        self.assertNotIn("v = v;", c)
        self.assertIn("return v;", c)

    # -- self-referential dead variable (liveness alone can't see it) ----
    def test_dead_self_referential_loop_variable_removed(self):
        # `unused` is read only to compute its own next value inside
        # the loop -- nothing outside the loop ever reads it, but a
        # plain backward-liveness analysis sees it as "live" at every
        # point in the loop body (it's read right there, by itself),
        # so a loop of this shape is the regression case for the
        # dependency-closure pass added to catch it.
        out = self.emitted(
            "int main(){ int unused = 0; int i; "
            "for (i = 0; i < 10; i++) { unused = unused + i; } "
            "return 0; }"
        )
        c = self.c_section(out)
        self.assertNotIn("unused", c)

    def test_used_and_dead_accumulators_distinguished_in_same_loop(self):
        # `sum` (returned) and `dead` (never read) are updated by
        # structurally identical self-referential statements in the
        # same loop body -- only `dead` (and its update) may be
        # removed; `sum` must survive untouched.
        out = self.emitted(
            "int main(){ int sum = 0; int dead = 0; int i; "
            "for (i = 0; i < 20; i++) { sum = sum + i; dead = dead + i * 2; } "
            "return sum % 1000; }"
        )
        c = self.c_section(out)
        self.assertNotIn("dead", c)
        self.assertIn("sum = sum + i;", c)

    # -- algebraic simplification identities ------------------------------
    def test_algebraic_identities(self):
        out = self.emitted(
            "int f(int x){ int a = x + 0; int b = x * 1; "
            "int c = x - 0; int d = 1 * x; return a + b + c + d; }"
            "int main(){ return f(7) % 256; }"
        )
        self.assertIn("simplified to", out)

    def test_multiply_by_zero_identity(self):
        out = self.emitted("int f(int x){ return x * 0; } int main(){ return f(9); }")
        self.assertIn("return 0;", self.c_section(out))

    # -- unused-variable declarations are dropped from the output --------
    def test_unused_variable_declaration_dropped(self):
        out = self.emitted("int main(){ int unused = 42; return 0; }")
        c = self.c_section(out)
        self.assertNotIn("unused", c)
        self.assertIn("return 0;", c)

    # -- constant propagation removes the intermediate variable ----------
    def test_constant_propagation_through_chain(self):
        out = self.emitted("int main(){ int a = 5; int b = a + 2; int c = b * 2; return c; }")
        c = self.c_section(out)
        self.assertIn("return 14;", c)
        self.assertNotIn(" a;", c)
        self.assertNotIn(" b;", c)
        self.assertNotIn(" c;", c)

    # -- optimisation statistics must match what was actually removed ----
    def test_stats_match_actual_transformations(self):
        out = self.emitted(
            "int f(int v){ int w = v; v = w; int dead = 1; return v; }"
            "int main(){ return f(3); }"
        )
        c = self.c_section(out)
        self.assertNotIn("v = v;", c)
        self.assertNotIn("dead", c)
        stats_section = out.split("Optimisation passes performed:")[1]
        m = re.search(r"Dead stores eliminated:\s*(\d+)", stats_section)
        self.assertIsNotNone(m)
        # w (unused once v=w is substituted), dead, and the v=v
        # self-assignment: three dead/no-op stores in total.
        self.assertEqual(int(m.group(1)), 3)


@unittest.skipUnless(CC, "no C compiler available")
class Differential(unittest.TestCase):
    def compile_and_run(self, src_path, workdir, tag):
        exe = os.path.join(workdir, tag)
        r = subprocess.run([CC, "-w", "-o", exe, src_path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"{src_path} failed to compile:\n{r.stderr}")
        return subprocess.run([exe]).returncode

    def test_programs_behave_identically(self):
        programs = sorted(glob(os.path.join(ROOT, "tests/programs/*.c")))
        self.assertTrue(programs)
        for prog in programs:
            with self.subTest(program=os.path.basename(prog)), \
                    tempfile.TemporaryDirectory() as d:
                opt = os.path.join(d, "opt.c")
                code, out = analyse(prog, out=opt)
                self.assertEqual(code, 0, out)
                self.assertEqual(self.compile_and_run(prog, d, "orig"),
                                 self.compile_and_run(opt, d, "opt"))


class PreprocessorTests(unittest.TestCase):
    def test_object_like_macro(self):
        out, errs = strip_directives("#define SIZE 10\nint x = SIZE;\n")
        self.assertFalse(errs)
        self.assertIn("int x = 10;", out)

    def test_function_like_macro(self):
        out, errs = strip_directives(
            "#define SQUARE(x) ((x) * (x))\nint y = SQUARE(5);\n"
        )
        self.assertFalse(errs)
        self.assertIn("((5) * (5))", out)

    def test_chained_macro_expansion(self):
        out, errs = strip_directives("#define A B\n#define B C\n#define C 42\nint x = A;\n")
        self.assertFalse(errs)
        self.assertIn("int x = 42;", out)

    def test_self_referential_macro_not_infinite(self):
        out, errs = strip_directives("#define SELF (SELF + 1)\nint y = SELF;\n")
        self.assertFalse(errs)
        self.assertIn("int y = (SELF + 1);", out)

    def test_undef_removes_macro(self):
        out, errs = strip_directives("#define X 1\n#undef X\nint x = X;\n")
        self.assertFalse(errs)
        self.assertIn("int x = X;", out)

    def test_ifdef_taken_branch(self):
        out, errs = strip_directives(
            "#define DEBUG\n#ifdef DEBUG\nint x = 1;\n#else\nint x = 2;\n#endif\n"
        )
        self.assertFalse(errs)
        self.assertIn("int x = 1;", out)
        self.assertNotIn("int x = 2;", out)

    def test_if_elif_else_chain(self):
        out, errs = strip_directives(
            "#define LEVEL 2\n"
            "#if LEVEL == 1\nint r = 10;\n"
            "#elif LEVEL == 2\nint r = 20;\n"
            "#elif LEVEL == 3\nint r = 30;\n"
            "#else\nint r = 0;\n#endif\n"
        )
        self.assertFalse(errs)
        self.assertIn("int r = 20;", out)
        self.assertNotIn("int r = 10;", out)
        self.assertNotIn("int r = 30;", out)

    def test_defined_operator(self):
        out, errs = strip_directives(
            "#define FEATURE\n#if defined(FEATURE)\nint x = 1;\n#endif\n"
        )
        self.assertFalse(errs)
        self.assertIn("int x = 1;", out)

    def test_line_numbers_preserved(self):
        out, errs = strip_directives("#define X 1\n\nint main() {\n    return X;\n}\n")
        self.assertFalse(errs)
        lines = out.split("\n")
        self.assertIn("return 1;", lines[3])

    def test_unterminated_if_reported(self):
        out, errs = strip_directives("int main() {\n#if 1\n    return 0;\n")
        self.assertTrue(any("unterminated #if" in str(e) for e in errs), errs)

    def test_division_by_zero_in_condition_reported(self):
        out, errs = strip_directives("#if 1 / 0\nint x;\n#endif\n")
        self.assertTrue(any("division by zero" in str(e) for e in errs), errs)

    def test_unknown_directive_reported(self):
        out, errs = strip_directives("#bogus\nint main(){ return 0; }\n")
        self.assertTrue(any("unknown preprocessor directive" in str(e) for e in errs), errs)


@unittest.skipUnless(CC, "no C compiler available")
class PreprocessorDifferential(unittest.TestCase):
    def differential(self, src):
        with tempfile.TemporaryDirectory() as d:
            orig = os.path.join(d, "orig.c")
            opt = os.path.join(d, "opt.c")
            with open(orig, "w") as fh:
                fh.write(src)
            code, out = analyse(orig, out=opt)
            self.assertEqual(code, 0, out)
            orig_exe, opt_exe = os.path.join(d, "a"), os.path.join(d, "b")
            r1 = subprocess.run([CC, "-w", "-o", orig_exe, orig], capture_output=True, text=True)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = subprocess.run([CC, "-w", "-o", opt_exe, opt], capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(subprocess.run([orig_exe]).returncode,
                              subprocess.run([opt_exe]).returncode)

    def test_macros_and_conditionals_match_gcc(self):
        self.differential(
            "#define SIZE 10\n"
            "#define SQUARE(x) ((x) * (x))\n"
            "int main() {\n"
            "    int arr[SIZE];\n"
            "    int y = SQUARE(5);\n"
            "    return y % 256;\n"
            "}\n"
        )

    def test_conditional_compilation_matches_gcc(self):
        self.differential(
            "#define LEVEL 2\n"
            "int main() {\n"
            "#if LEVEL == 1\n    int r = 10;\n"
            "#elif LEVEL == 2\n    int r = 20;\n"
            "#else\n    int r = 0;\n#endif\n"
            "    return r;\n"
            "}\n"
        )

    def test_local_include_matches_gcc(self):
        with tempfile.TemporaryDirectory() as d:
            header = os.path.join(d, "consts.h")
            with open(header, "w") as fh:
                fh.write("#define GREETING_LEN 5\n")
            src = os.path.join(d, "main.c")
            with open(src, "w") as fh:
                fh.write(
                    '#include "consts.h"\n'
                    "int main() { return GREETING_LEN * 2; }\n"
                )
            opt = os.path.join(d, "opt.c")
            code, out = analyse(src, out=opt)
            self.assertEqual(code, 0, out)
            orig_exe, opt_exe = os.path.join(d, "a"), os.path.join(d, "b")
            r1 = subprocess.run([CC, "-w", "-o", orig_exe, src], capture_output=True, text=True)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = subprocess.run([CC, "-w", "-o", opt_exe, opt], capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(subprocess.run([orig_exe]).returncode,
                              subprocess.run([opt_exe]).returncode)


@unittest.skipUnless(CC, "no C compiler available")
class RegressionSuite(unittest.TestCase):
    """Dedicated coverage for each scenario in the project brief's
    validation task (A-H), plus the brief's own TASK 3 sample program.
    Each case that compiles is verified by actually running both the
    original and the optimised C through the system compiler and
    comparing exit codes -- semantic equivalence, not just text."""

    def compile_and_run(self, src_path, workdir, tag):
        exe = os.path.join(workdir, tag)
        r = subprocess.run([CC, "-w", "-o", exe, src_path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"{src_path} failed to compile:\n{r.stderr}")
        return subprocess.run([exe]).returncode

    def differential(self, src):
        with tempfile.TemporaryDirectory() as d:
            orig = os.path.join(d, "orig.c")
            opt = os.path.join(d, "opt.c")
            with open(orig, "w") as fh:
                fh.write(src)
            code, out = analyse(orig, out=opt)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.compile_and_run(orig, d, "a"),
                              self.compile_and_run(opt, d, "b"))
            return out

    # A: constant folding
    def test_a_constant_folding(self):
        out = self.differential("int main() { int x = 2 + 3; return x; }")
        self.assertIn("'2 + 3' evaluated at compile time -> 5", out)

    # B: constant propagation
    def test_b_constant_propagation(self):
        out = self.differential("int main() { int x = 5; int y = x + 2; return y; }")
        self.assertIn("return 7;", out.split("OPTIMISED C CODE")[1])

    # C: dead code elimination
    def test_c_dead_code_elimination(self):
        out = self.differential("int main() { int x = 10; return 0; }")
        c = out.split("OPTIMISED C CODE")[1]
        self.assertNotIn(" x;", c)
        self.assertIn("return 0;", c)

    # D: conditional statements
    def test_d_conditional_statements(self):
        self.differential(
            "int main() { int x = 10; if (x > 5) { return 1; } else { return 0; } }"
        )

    # E: multiple functions
    def test_e_multiple_functions(self):
        self.differential(
            "int add(int a, int b) { return a + b; } "
            "int main() { int result = add(2, 3); return 0; }"
        )

    # F: invalid input -- rejected with a clear error, nothing miscompiled
    def test_f_invalid_input_rejected(self):
        code, out = analyse(os.path.join(ROOT, "tests/sample_syntax_error.c"))
        self.assertEqual(code, 1)
        self.assertNotIn("OPTIMISED C CODE", out)

    # G: side-effect preservation (a call with an observable global write
    # must survive even though its own return value is unused)
    def test_g_side_effects_preserved(self):
        out = self.differential(
            "int g = 0; "
            "int bump() { g = g + 1; return g; } "
            "int main() { bump(); bump(); return g; }"
        )
        c = out.split("OPTIMISED C CODE")[1]
        self.assertIn("bump();", c)

    # H: nested if/else, early returns, and the unreachable code after
    # a branch that always returns
    def test_h_control_flow_preservation(self):
        out = self.differential(
            "int classify(int x) { "
            "  if (x > 0) { if (x > 100) { return 2; } return 1; } "
            "  else { return 0; } "
            "  return 99; } "
            "int main() { return classify(150) + classify(-1) + classify(5); }"
        )
        self.assertIn("unreachable", out)

    def test_task3_sample_program(self):
        src = (
            "int compute(int a, int b) {\n"
            "    int c1 = 2 + 3;\n"
            "    int sum = a + b;\n"
            "    if (sum > 100) {\n"
            "        return sum - 1;\n"
            "    }\n"
            "    return sum + c1;\n"
            "}\n"
            "int main() {\n"
            "    int value = compute(10, 20);\n"
            "    return 0;\n"
            "}\n"
        )
        out = self.differential(src)
        c = out.split("OPTIMISED C CODE")[1]
        # `c1 = 2 + 3` is folded, then propagated *across* the if into the
        # final return (cross-block constant propagation), leaving c1 dead
        self.assertIn("return sum + 5;", c)
        self.assertNotIn("c1", c)
        self.assertIn("sum = a + b;", c)
        self.assertIn("sum > 100", c)
        self.assertIn("compute(10, 20);", c)
        self.assertNotIn("value", c)

    def test_task3_sample_multiple_inputs(self):
        # compute()'s result can't be folded for an arbitrary (a, b),
        # so check the compiled optimised code actually agrees with the
        # original across both branches of the `if`, not just the one
        # (10, 20) call baked into the sample.
        template = (
            "int compute(int a, int b) {{\n"
            "    int c1 = 2 + 3;\n"
            "    int sum = a + b;\n"
            "    if (sum > 100) {{ return sum - 1; }}\n"
            "    return sum + c1;\n"
            "}}\n"
            "int main() {{ return compute({a}, {b}) % 256; }}\n"
        )
        for a, b in [(10, 20), (0, 0), (90, 20), (-5, 3), (50, 51)]:
            with self.subTest(a=a, b=b):
                self.differential(template.format(a=a, b=b))

    # I: redundant self-assignment, including ones produced by other
    # passes rather than written directly in the source
    def test_i_redundant_self_assignment(self):
        out = self.differential(
            "int id(int x) { x = x; return x; } "
            "int main() { return id(5); }"
        )
        self.assertNotIn("x = x;", out.split("OPTIMISED C CODE")[1])

    def test_i_self_assignment_from_copy_propagation(self):
        out = self.differential(
            "int f(int v) { int w = v; v = w; return v; } "
            "int main() { return f(3); }"
        )
        self.assertNotIn("v = v;", out.split("OPTIMISED C CODE")[1])

    # J: a variable whose only reader is itself inside a loop (plain
    # backward liveness can't see this is dead -- it looks live at
    # every point in the loop body, since it's read right there)
    def test_j_dead_self_referential_loop_variable(self):
        out = self.differential(
            "int main() { int unused = 0; int i; "
            "for (i = 0; i < 10; i++) { unused = unused + i; } "
            "return 0; }"
        )
        self.assertNotIn("unused", out.split("OPTIMISED C CODE")[1])

    def test_j_live_and_dead_accumulators_in_same_loop(self):
        out = self.differential(
            "int main() { int sum = 0; int dead = 0; int i; "
            "for (i = 0; i < 20; i++) { sum = sum + i; dead = dead + i * 2; } "
            "return sum % 1000; }"
        )
        c = out.split("OPTIMISED C CODE")[1]
        self.assertNotIn("dead", c)
        self.assertIn("sum", c)

    # K: nested conditions inside a loop with break/continue, and a
    # loop-carried accumulator -- control flow must stay correct and
    # the real accumulator must not be mistaken for dead
    def test_k_nested_conditions_with_break_and_continue(self):
        self.differential(
            "int main() { int sum = 0; int i; "
            "for (i = 0; i < 10; i++) { "
            "  if (i == 5) { continue; } "
            "  if (i == 8) { break; } "
            "  sum = sum + i; "
            "} "
            "return sum; }"
        )

    # L: algebraic simplification composed with dead code elimination
    # and a genuine side effect (the call must survive either way)
    def test_l_algebraic_simplification_with_side_effect(self):
        out = self.differential(
            "int g = 0; "
            "int bump() { g = g + 1; return g; } "
            "int main() { int x = bump() + 0; int y = x * 1; return y % 256; }"
        )
        c = out.split("OPTIMISED C CODE")[1]
        self.assertIn("bump()", c)


@unittest.skipUnless(CC, "no C compiler available")
class MixedMemberIndexChains(unittest.TestCase):
    """A struct field that is itself an array, and arrays of structs
    with array fields, both accessed through `.`/`->` immediately
    followed by `[...]` -- e.g. `b.vals[i]`, `p->vals[i]`,
    `boxes[i].vals[j]`. The emitter represents these as one composite
    operand string (same trick already used for plain `a[i]` and
    `s.f`), so this is also a regression test for the chain-decoding
    logic in emitter.py that has to tell indexing and member-access
    steps apart in the right order, including materialising a
    deferred temporary that appears *inside* one of the index
    expressions (`b.vals[i + 1]` below)."""

    def compile_and_run(self, src_path, workdir, tag):
        exe = os.path.join(workdir, tag)
        r = subprocess.run([CC, "-w", "-o", exe, src_path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"{src_path} failed to compile:\n{r.stderr}")
        return subprocess.run([exe]).returncode

    def differential(self, src):
        with tempfile.TemporaryDirectory() as d:
            orig = os.path.join(d, "orig.c")
            opt = os.path.join(d, "opt.c")
            with open(orig, "w") as fh:
                fh.write(src)
            code, out = analyse(orig, out=opt)
            self.assertEqual(code, 0, out)
            self.assertEqual(self.compile_and_run(orig, d, "a"),
                              self.compile_and_run(opt, d, "b"))
            return out

    def test_struct_array_field_via_dot(self):
        self.differential(
            "struct Box { int vals[3]; }; "
            "int main() { struct Box b; int i = 1; "
            "b.vals[i] = 5; b.vals[i + 1] = 7; "
            "return b.vals[i] + b.vals[i + 1]; }"
        )

    def test_struct_array_field_via_arrow(self):
        self.differential(
            "struct Box { int vals[3]; }; "
            "int main() { struct Box b; struct Box *p = &b; "
            "p->vals[0] = 42; return p->vals[0]; }"
        )

    def test_array_of_structs_with_array_field(self):
        self.differential(
            "struct Box { int vals[2]; }; "
            "struct Boxes { struct Box items[2]; }; "
            "int main() { struct Boxes bs; "
            "bs.items[0].vals[0] = 100; bs.items[1].vals[1] = 200; "
            "return (bs.items[0].vals[0] + bs.items[1].vals[1]) % 256; }"
        )

    def test_address_of_and_increment_struct_member(self):
        self.differential(
            "struct Point { int x; int y; }; "
            "void inc(int *p) { *p = *p + 1; } "
            # p.y must be initialised: `p.y++` on an uninitialised member
            # is undefined behaviour, so its result is stack garbage and
            # the original/optimised comparison was only ever luck.
            "int main() { struct Point p; p.x = 5; p.y = 1; "
            "inc(&p.x); p.y++; ++p.x; return p.x + p.y; }"
        )

    def test_struct_by_value_param_and_sizeof(self):
        self.differential(
            "struct Point { int x; int y; }; "
            "int sum_point(struct Point p) { return p.x + p.y; } "
            "int main() { struct Point a; a.x = 3; a.y = 4; "
            "int s = sum_point(a); int sz = sizeof(struct Point); "
            "return (s + sz) % 256; }"
        )

    def test_preprocessor_struct_and_multidim_array_together(self):
        # A (macros + conditional compilation), B (struct with an
        # array field) and C (a plain 2-D array) all exercised in one
        # program, confirming none of the three features interferes
        # with the others end to end through the real C compiler.
        self.differential(
            "#define N 3\n"
            "#define SQ(x) ((x)*(x))\n"
            "struct Vec { int data[N]; };\n"
            "#ifdef DEBUG\n"
            "int mode = 1;\n"
            "#else\n"
            "int mode = 0;\n"
            "#endif\n"
            "int main() {\n"
            "    struct Vec v;\n"
            "    int i;\n"
            "    for (i = 0; i < N; i++) { v.data[i] = SQ(i); }\n"
            "    int grid[2][2];\n"
            "    grid[0][0] = v.data[0];\n"
            "    grid[1][1] = v.data[2];\n"
            "    return (v.data[1] + grid[0][0] + grid[1][1] + mode) % 256;\n"
            "}\n"
        )


# ---------------------------------------------------------------------
# Parser regressions: valid C that used to be rejected, and invalid C
# that must still be rejected -- once, at the right place.
# ---------------------------------------------------------------------
def parse_errors(src):
    parser = Parser(Lexer(src).tokenize())
    parser.parse()
    return [str(e) for e in parser.errors]


def run_pipeline(src):
    """Write src to a temp file, run the full tool on it and return
    (exit code, report text, optimised C text or None)."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "in.c")
        out = os.path.join(d, "out.c")
        with open(path, "w") as f:
            f.write(src)
        code, text = analyse(path, out=out)
        opt = None
        if os.path.exists(out):
            with open(out) as f:
                opt = f.read()
        return code, text, opt


def build_and_run(path, workdir, tag):
    exe = os.path.join(workdir, tag)
    r = subprocess.run([CC, "-w", "-o", exe, path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([exe], capture_output=True, text=True, timeout=10)
    return r.stdout, r.returncode


class ParserAcceptsValidC(unittest.TestCase):
    def assertParses(self, src):
        self.assertEqual(parse_errors(src), [])

    def test_array_initialiser_with_indexing(self):
        self.assertParses("int main() { int arr[5] = {10,20,30,40,50}; return arr[2]; }")

    def test_array_initialiser_trailing_comma_and_unsized(self):
        self.assertParses("int main() { int a[] = {1, 2, 3,}; return a[1]; }")

    def test_nested_initialiser(self):
        self.assertParses("int main() { int g[2][2] = {{1,2},{3,4}}; return g[1][0]; }")

    def test_for_loop_forms(self):
        self.assertParses("int main() { int i; int s = 0;"
                          " for (i = 0; i < 5; i++) { s += i; }"
                          " for (;;) { break; }"
                          " for (int j = 0; j < 3; j++) s += j;"
                          " return s; }")

    def test_pointers(self):
        self.assertParses("int main() { int x = 5; int *p = &x; *p = 7; int **q = &p;"
                          " return **q + *p; }")

    def test_function_definitions_and_calls(self):
        code, out, _ = run_pipeline("#include <stdio.h>\n"
                                    "int square(int n);\n"
                                    "int square(int n) { return n * n; }\n"
                                    "int main() { printf(\"%d\\n\", square(4)); return 0; }\n")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Error", out)

    def test_nested_blocks(self):
        self.assertParses("int main() { int a = 1; { int a = 2; { int a = 3; } } return a; }")

    def test_the_original_failing_program(self):
        with open(os.path.join(ROOT, "tests/programs/parser_sum_pointer.c")) as f:
            src = f.read()
        code, out, _ = run_pipeline(src)
        self.assertEqual(code, 0, out)
        self.assertNotIn("Syntax Error", out)


class ParserRejectsInvalidC(unittest.TestCase):
    def only_error(self, src, line, col=None, text=None):
        errs = parse_errors(src)
        self.assertEqual(len(errs), 1, errs)           # exactly one, no cascade
        self.assertIn(f"(line {line})", errs[0])
        if col is not None:
            self.assertIn(f"[col {col}]", errs[0])
        if text:
            self.assertIn(text, errs[0])

    def test_missing_semicolon_points_at_next_token(self):
        self.only_error("int main() {\n    int a = 5\n    return a;\n}\n",
                        3, 5, "expected ';'")

    def test_two_missing_semicolons_two_errors(self):
        errs = parse_errors("int main() {\n int a = 5\n int b = 10\n return a + b;\n}\n")
        self.assertEqual(len(errs), 2, errs)
        self.assertIn("(line 3)", errs[0])
        self.assertIn("(line 4)", errs[1])

    def test_missing_paren(self):
        self.only_error("int main() {\n    int a = 1;\n    if (a > 0 {\n        a = 2;\n    }\n    return a;\n}\n",
                        3, 15, "expected ')'")

    def test_unclosed_block_reports_where_it_was_opened(self):
        errs = parse_errors("int main() {\n    int a = 1;\n    return a;\n")
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("block opened at line 1", errs[0])

    def test_bad_initialiser_list_no_cascade(self):
        self.only_error("int main() {\n    int a[3] = {1, 2 3};\n    return a[0];\n}\n",
                        2, 22, "initialiser list")

    def test_designated_initialiser_is_rejected_not_misparsed(self):
        self.only_error("int main() {\n    int a[3] = {[1] = 5};\n    return a[1];\n}\n",
                        2, None, "designated initialisers")

    def test_for_header_missing_semicolon(self):
        self.only_error("int main() {\n    int i;\n    int s = 0;\n"
                        "    for (i = 0; i < 3 i++) { s += i; }\n    return s;\n}\n",
                        4, None, "expected ';'")

    def test_missing_expression(self):
        self.only_error("int main() {\n    int a = 1;\n    a = ;\n    return a;\n}\n",
                        3, None, "expected an expression")

    def test_brace_expression_outside_declaration(self):
        errs = parse_errors("int main() {\n    int a;\n    a = {1, 2};\n    return a;\n}\n")
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("(line 3)", errs[0])

    def test_empty_inner_dimension(self):
        self.only_error("int main() {\n    int a[2][] = {{1,2,3}};\n    return 0;\n}\n",
                        2, None, "first array dimension")

    def test_errors_do_not_hide_later_valid_code(self):
        # an error in f must not stop g from being parsed or swallow it
        errs = parse_errors("int f() { int a = ; return a; }\n"
                            "int g() { return 1 }\n")
        self.assertEqual(len(errs), 2, errs)
        self.assertIn("(line 1)", errs[0])
        self.assertIn("(line 2)", errs[1])


class ProgramsBehaveTheSame(unittest.TestCase):
    """Compile every program in tests/programs as written and as emitted by
    the optimiser with the real C compiler, then compare *stdout and exit
    status* (the older Differential test only compares exit status)."""

    def test_stdout_and_exit_code_match(self):
        programs = sorted(glob(os.path.join(ROOT, "tests/programs/*.c")))
        self.assertGreater(len(programs), 30)
        for prog in programs:
            with self.subTest(program=os.path.basename(prog)), tempfile.TemporaryDirectory() as d:
                opt = os.path.join(d, "opt.c")
                code, out = analyse(prog, out=opt)
                self.assertEqual(code, 0, out)
                self.assertEqual(build_and_run(prog, d, "orig"),
                                 build_and_run(opt, d, "opt"))


class ConstantFoldingStillWorks(unittest.TestCase):
    def test_folds_and_matches(self):
        src = ("#include <stdio.h>\n"
               "int main() {\n"
               "    int a = 2 + 3 * 4;\n"
               "    int b = a - 14;\n"
               "    int c = (10 / 2) % 3;\n"
               "    printf(\"%d %d %d\\n\", a, b, c);\n"
               "    return a + b + c;\n"
               "}\n")
        code, out, opt = run_pipeline(src)
        self.assertEqual(code, 0, out)
        with tempfile.TemporaryDirectory() as d:
            orig = os.path.join(d, "o.c")
            optp = os.path.join(d, "p.c")
            with open(orig, "w") as f:
                f.write(src)
            with open(optp, "w") as f:
                f.write(opt)
            self.assertEqual(build_and_run(orig, d, "a"), build_and_run(optp, d, "b"))
        self.assertNotIn("2 + 3", opt)


# ---------------------------------------------------------------------
# Optimiser behaviour: what is applied, what is deliberately not applied,
# and what the report says about each.
# ---------------------------------------------------------------------
from smartc.pipeline import analyze   # noqa: E402


def optimised(src):
    res = analyze(src)
    assert res["success"], res
    return res


def same_behaviour(testcase, src):
    """Original and optimised C produce the same stdout and exit code."""
    res = optimised(src)
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "a.c"), os.path.join(d, "b.c")
        with open(a, "w") as f:
            f.write(src)
        with open(b, "w") as f:
            f.write(res["optimised_c"])
        testcase.assertEqual(build_and_run(a, d, "a"), build_and_run(b, d, "b"))
    return res


class UserCalculateExample(unittest.TestCase):
    with open(os.path.join(ROOT, "tests/programs/opt_calculate_example.c")) as _f:
        SRC = _f.read()

    def test_emits_the_expected_clean_c(self):
        res = same_behaviour(self, self.SRC)
        c = res["optimised_c"]
        self.assertIn("#include <stdio.h>", c)
        self.assertIn("int calculate(int x, int y) {", c)
        # 10+20 and 5*4 are folded, and the single-use locals are forwarded
        # into the return expression instead of being kept as temporaries.
        self.assertIn("return 50 + (x + y);", c)
        self.assertNotIn("final_result", c)
        self.assertIn("int main() {", c)
        for gone in ("unused", "duplicate", "999", "1000", "if ("):
            self.assertNotIn(gone, c)

    def test_no_separate_declaration_left_behind(self):
        c = optimised(self.SRC)["optimised_c"]
        self.assertNotIn("int result", c)
        self.assertNotIn("int final_result", c)


class OptimiserBehaviour(unittest.TestCase):
    def test_constant_propagates_across_a_branch(self):
        src = ("int f(int c) {\n int x = 5;\n if (c) { c = c + 1; }\n return x + c;\n}\n"
               "int main() { return f(2); }\n")
        res = same_behaviour(self, src)
        self.assertIn("return 5 + c;", res["optimised_c"])

    def test_constant_changed_in_a_loop_is_not_propagated(self):
        src = ("int main() {\n int s = 0; int i;\n for (i = 0; i < 4; i++) { s = s + i; }\n"
               " return s;\n}\n")
        res = same_behaviour(self, src)
        self.assertNotIn("return 0;", res["optimised_c"])

    def test_constant_condition_removes_dead_branch(self):
        src = ("int main() {\n int r = 1;\n if (0) { r = 99; }\n if (1) { r = r + 2; }\n return r;\n}\n")
        res = same_behaviour(self, src)
        self.assertNotIn("99", res["optimised_c"])
        self.assertIn("return 3;", res["optimised_c"])

    def test_int_division_truncates_toward_zero(self):
        res = same_behaviour(self, "int main() { int a = -7 / 2; int b = -7 % 2; return a * 10 + b + 100; }\n")
        self.assertIn("return 69;", res["optimised_c"])        # -3*10 + -1 + 100

    def test_int_and_double_constants_keep_their_types(self):
        src = ('#include <stdio.h>\nint main() { double d = 1; int i = 2.7;\n'
               ' printf("%f %d %f\\n", d / 2, i, 3 / 2 + 0.5); return 0; }\n')
        res = same_behaviour(self, src)
        self.assertIn("0.5", res["optimised_c"])

    def test_assignment_to_float_variable_is_not_substituted(self):
        # a float holds a rounded value; substituting the double literal
        # would change later arithmetic
        src = '#include <stdio.h>\nint main() { float f = 0.1; double d = f * 3; printf("%.12f\\n", d); return 0; }\n'
        same_behaviour(self, src)

    def test_short_circuit_still_guards_the_dereference(self):
        src = ("#include <stdio.h>\n#include <stdlib.h>\n"
               "int main() { int *p = NULL; int ok = 0;\n"
               " if (p != NULL && *p > 0) { ok = 1; }\n printf(\"%d\\n\", ok); return 0; }\n")
        res = same_behaviour(self, src)      # would crash if *p were evaluated eagerly
        c = res["optimised_c"]
        # the guard is still there: the dereference sits inside `if (p != NULL)`
        self.assertRegex(c, r"if \(p != NULL\) \{\s+if \(\*p > 0\)")
        self.assertNotIn("goto", c)

    def test_short_circuit_skips_a_call_with_side_effects(self):
        src = ('#include <stdio.h>\nint hits = 0;\nint bump() { hits = hits + 1; return 1; }\n'
               'int main() { int a = 0; int r = a && bump(); int s = 1 || bump();\n'
               ' printf("%d %d %d\\n", r, s, hits); return 0; }\n')
        res = same_behaviour(self, src)
        self.assertIn("bump", res["optimised_c"])

    def test_cheap_pure_condition_stays_straight_line(self):
        src = "int f(int a, int b, int c) { return a < b && b < c; }\nint main() { return f(1, 2, 3); }\n"
        res = same_behaviour(self, src)
        self.assertNotIn("goto", res["optimised_c"])

    def test_cse_does_not_reuse_a_value_after_compound_assignment(self):
        src = ('#include <stdio.h>\nint main() { int c = 5, d = 6; c += d;\n'
               ' printf("%d\\n", c + d); return 0; }\n')
        same_behaviour(self, src)

    def test_copy_between_different_types_is_not_propagated(self):
        src = ('#include <stdio.h>\ndouble widen(int a) { double d = a; return d / 4; }\n'
               'int main() { printf("%f\\n", widen(3)); return 0; }\n')
        res = same_behaviour(self, src)
        self.assertNotIn("a / 4", res["optimised_c"])

    def test_declaration_and_first_assignment_are_merged(self):
        # y is read twice, so it must stay a real variable; its declaration
        # and first assignment are emitted as one statement.
        res = optimised("int f(int a) { int y; y = 3 + a; return y * y; }\nint main() { return f(1); }\n")
        self.assertIn("int y = 3 + a;", res["optimised_c"])
        self.assertNotIn("int y;", res["optimised_c"])

    def test_iteration_reaches_a_fixed_point(self):
        with open(os.path.join(ROOT, "tests/programs/opt_dataflow.c")) as f:
            res = optimised(f.read())
        self.assertTrue(res["optimisation_stats"]["converged"])
        self.assertLess(res["optimisation_stats"]["passes_run"], 30)


class OptimisationReport(unittest.TestCase):
    def report(self, src):
        return optimised(src)["optimisation_report"]

    def test_report_has_three_sections(self):
        rep = self.report("int main() { int a = 2 + 3; return a; }\n")
        self.assertEqual(set(rep), {"applied", "detected_not_applied", "unsupported"})
        self.assertTrue(rep["applied"])
        self.assertTrue(rep["unsupported"])

    def test_float_identity_is_detected_but_not_applied(self):
        src = ("double f(double x) { return x * 0; }\nint main() { return (int)f(2.0); }\n")
        res = optimised(src)
        rep = res["optimisation_report"]
        self.assertTrue(any("x * 0" in e["message"] for e in rep["detected_not_applied"]), rep)
        self.assertFalse(any("x * 0" in e["message"] for e in rep["applied"]))
        self.assertIn("x * 0", res["optimised_c"])          # really left alone

    def test_integer_identity_is_applied(self):
        res = optimised("int f(int x) { return x * 1 + 0; }\nint main() { return f(4); }\n")
        self.assertTrue(any(e["label"].startswith("Algebraic") for e in res["optimisation_report"]["applied"]))
        self.assertNotIn("* 1", res["optimised_c"])

    def test_overflowing_constant_expression_is_not_folded(self):
        src = "int main() { int a = 2147483647 + 1; return a != 0; }\n"
        rep = self.report(src)
        self.assertTrue(any("overflows int" in e["message"] for e in rep["detected_not_applied"]), rep)
        self.assertIn("2147483647 + 1", optimised(src)["optimised_c"])

    def test_division_by_zero_constant_is_not_folded(self):
        src = "int main() { int z = 0; int a = 5; if (z) { a = 1 / z; } return a; }\n"
        optimised(src)         # must not crash the compiler or the tool
        src2 = "int main() { int a = 1; if (a == 2) { a = 7 / 0; } return a; }\n"
        rep = self.report(src2)
        self.assertTrue(rep["detected_not_applied"] or rep["applied"])

    def test_unsupported_list_never_claims_what_is_implemented(self):
        unsupported = " ".join(self.report("int main() { return 0; }\n")["unsupported"]).lower()
        for implemented in ("constant folding", "dead code", "copy propagation"):
            self.assertNotIn(implemented, unsupported)

    def test_cli_prints_all_three_sections(self):
        code, out = analyse(os.path.join(ROOT, "tests/programs/opt_calculate_example.c"))
        self.assertEqual(code, 0)
        for heading in ("APPLIED", "DETECTED BUT NOT APPLIED", "NOT SUPPORTED BY THIS TOOL"):
            self.assertIn(heading, out)


def program_text(name):
    with open(os.path.join(ROOT, "tests", "programs", name)) as f:
        return f.read()


def run_both(testcase, src):
    """Compile and run original and optimised C; return both results."""
    res = optimised(src)
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "a.c"), os.path.join(d, "b.c")
        with open(a, "w") as f:
            f.write(src)
        with open(b, "w") as f:
            f.write(res["optimised_c"])
        return res, build_and_run(a, d, "a"), build_and_run(b, d, "b")


class StructuredControlFlow(unittest.TestCase):
    """Loops and branches come back out as `for` / `while` / `if`, with the
    IR's labels and jumps kept internal."""

    LEAK = re.compile(r"\bL_(while|for|forcont|endwhile|endfor|else|endif|sc)\d+\b")

    def test_for_and_while_loops_are_preserved(self):
        res, orig, opt = run_both(self, program_text("loops_structured.c"))
        self.assertEqual(orig, opt)
        c = res["optimised_c"]
        self.assertIn("for (int i = 1; i <= n; i = i + 1) {", c)
        self.assertIn("while (n > 0) {", c)
        self.assertIn("while (1) {", c)               # the explicit `while (1)`
        self.assertIn("break;", c)
        self.assertIn("continue;", c)
        self.assertNotIn("goto", c)
        self.assertIsNone(self.LEAK.search(c))

    def test_break_only_leaves_the_innermost_loop(self):
        src = ('#include <stdio.h>\nint main() { int n = 0;\n'
               ' for (int i = 0; i < 4; i++) { for (int j = 0; j < 4; j++) {\n'
               '   if (j == 2) { break; } n = n + 1; } }\n'
               ' printf("%d\\n", n); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "8")
        self.assertNotIn("goto", res["optimised_c"])

    def test_continue_in_a_for_loop_still_runs_the_update(self):
        src = ('#include <stdio.h>\nint main() { int s = 0;\n'
               ' for (int i = 0; i < 6; i++) { if (i % 2 == 0) { continue; } s = s + i; }\n'
               ' printf("%d\\n", s); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "9")
        self.assertIn("continue;", res["optimised_c"])

    def test_else_if_chain_is_rebuilt(self):
        res, orig, opt = run_both(self, program_text("loops_calls.c"))
        self.assertEqual(orig, opt)
        self.assertIn("} else if (x == 0) {", res["optimised_c"])

    def test_array_and_pointer_loops(self):
        res, orig, opt = run_both(self, program_text("loops_arrays_pointers.c"))
        self.assertEqual(orig, opt)
        c = res["optimised_c"]
        self.assertIn("for (int i = 0; i < n; i = i + 1) {", c)
        self.assertIn("a[i] = i * i;", c)
        self.assertIn("while (p != end) {", c)
        self.assertNotIn("goto", c)

    def test_pointer_guard_inside_a_loop_condition_is_kept(self):
        # `p != NULL && p[i] >= 0` must not read p[i] when p is NULL
        res, orig, opt = run_both(self, program_text("loops_arrays_pointers.c"))
        self.assertEqual(orig, opt)
        self.assertRegex(res["optimised_c"], r"if \(p != NULL && i < n\) \{\s+if \(p\[i\] >= 0\)")

    def test_function_calls_in_loop_conditions_run_every_time(self):
        res, orig, opt = run_both(self, program_text("loops_calls.c"))
        self.assertEqual(orig, opt)
        self.assertIn("next(", res["optimised_c"])

    def test_no_internal_label_or_goto_in_any_test_program(self):
        for path in sorted(glob(os.path.join(ROOT, "tests", "programs", "*.c"))):
            with open(path) as f:
                res = analyze(f.read())
            if not res["success"]:
                continue
            with self.subTest(program=os.path.basename(path)):
                self.assertIsNone(self.LEAK.search(res["optimised_c"]))
                self.assertEqual(res["control_flow"]["gotos"], 0)

    def test_control_flow_summary_is_counted_from_the_output(self):
        res = optimised(program_text("loops_structured.c"))
        c = res["optimised_c"]
        counted = len(re.findall(r"^\s*(?:for|while) \(", c, re.M))
        self.assertEqual(res["control_flow"]["loops"], counted)
        self.assertEqual(res["control_flow"]["gotos"], len(re.findall(r"\bgoto\b", c)))

    def test_ir_still_uses_labels_and_jumps(self):
        # the structure is recovered at emission time; the IR is untouched
        res = optimised(program_text("loops_structured.c"))
        self.assertIn("goto", res["ir_before"])
        self.assertIn("L_while", res["ir_before"])


class StructurerFallbacks(unittest.TestCase):
    """Unit tests for the emitter's structuring step on hand-built input:
    anything it cannot re-spell exactly must stay as labels and gotos."""

    def setUp(self):
        from smartc.structurer import Item, structure
        self.Item, self.structure = Item, structure

    def text(self, items):
        lines, _ = self.structure(items)
        return "\n".join(lines)

    def test_simple_while_is_structured(self):
        I = self.Item
        out = self.text([
            I("label", target="L_while0"),
            I("iffalse", "i < 3", target="L_endwhile1", prec=4),
            I("stmt", "i = i + 1;", expr=True),
            I("goto", target="L_while0"),
            I("label", target="L_endwhile1"),
        ])
        self.assertIn("while (i < 3) {", out)
        self.assertNotIn("goto", out)
        self.assertNotIn("L_", out)

    def test_a_jump_from_outside_keeps_the_function_in_goto_form(self):
        I = self.Item
        out = self.text([
            I("goto", target="L_else0"),                 # something else jumps to this label
            I("iffalse", "c", target="L_else0"),
            I("stmt", "x = 1;", expr=True),
            I("label", target="L_else0"),
            I("stmt", "y = 2;", expr=True),
        ])
        self.assertIn("goto L_else0;", out)
        self.assertIn("L_else0:;", out)
        self.assertNotIn("if (c) {", out)

    def test_a_declaration_used_after_the_block_is_not_wrapped_in_braces(self):
        I = self.Item
        out = self.text([
            I("iffalse", "c", target="L_else0"),
            I("stmt", "int v = 1;", decl="v"),
            I("label", target="L_else0"),
            I("stmt", "use(v);", expr=True),
        ])
        self.assertIn("goto L_else0;", out)            # left as a jump rather than hiding `v`

    def test_for_header_not_given_a_variable_used_after_the_loop(self):
        I = self.Item
        out = self.text([
            I("stmt", "int i = 0;", decl="i"),
            I("label", target="L_for0"),
            I("iffalse", "i < 3", target="L_endfor2", prec=4),
            I("stmt", "work();", expr=True),
            I("label", target="L_forcont1"),
            I("stmt", "i = i + 1;", expr=True),
            I("goto", target="L_for0"),
            I("label", target="L_endfor2"),
            I("stmt", "use(i);", expr=True),
        ])
        self.assertIn("int i = 0;", out)
        self.assertIn("for (; i < 3; i = i + 1) {", out)
        self.assertNotIn("for (int i", out)

    def test_loop_with_unexpected_exit_label_is_left_alone(self):
        I = self.Item
        out = self.text([
            I("label", target="L_while0"),
            I("iffalse", "c", target="L_endwhile1"),
            I("goto", target="L_while0"),
            I("stmt", "stray();", expr=True),                # something between back edge and exit
            I("label", target="L_endwhile1"),
        ])
        self.assertIn("goto", out)


class OptimiserSafetyRegressions(unittest.TestCase):
    """Self-assignments, temporaries, dead code and honesty of the report."""

    def test_self_assignments_removed_only_when_proven_safe(self):
        res, orig, opt = run_both(self, program_text("opt_self_assign.c"))
        self.assertEqual(orig, opt)
        c = res["optimised_c"]
        self.assertNotIn("*ptr = *ptr", c)
        self.assertNotIn("arr[2] = arr[2]", c)
        self.assertNotIn("s.x = s.x", c)
        self.assertIn("arr[k] = arr[k]", c)                 # index not proven in range: kept
        kept = " ".join(e["message"] for e in res["optimisation_report"]["detected_not_applied"])
        self.assertIn("arr[k] = arr[k]", kept)

    def test_parameter_pointer_self_assignment_is_kept(self):
        # `*p = *p` for a parameter that may be NULL: removing it would turn a crash into success
        src = ('#include <stdio.h>\nvoid touch(int *p) { *p = *p; }\n'
               'int main() { int x = 4; touch(&x); printf("%d\\n", x); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertIn("*p = *p", res["optimised_c"])

    def test_null_pointer_self_assignment_still_crashes(self):
        src = ('#include <stdio.h>\nvoid touch(int *p) { *p = *p; }\n'
               'int main() { touch(NULL); printf("survived\\n"); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertNotEqual(orig[1], 0)                      # it really does crash

    def test_variable_used_only_to_return_an_expression_is_removed(self):
        res, orig, opt = run_both(self, program_text("opt_loops_baseline.c"))
        self.assertEqual(orig, opt)
        c = res["optimised_c"]
        self.assertIn("return total * 2;", c)
        self.assertNotRegex(c, r"\bresult\b")

    def test_temporary_is_kept_when_it_is_read_twice(self):
        src = ('#include <stdio.h>\nint f(int a) { int y = a + 1; return y * y; }\n'
               'int main() { printf("%d\\n", f(3)); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertIn("int y = a + 1;", res["optimised_c"])

    def test_temporary_is_not_moved_past_a_write_to_what_it_reads(self):
        src = ('#include <stdio.h>\nint main() { int a = 1; int t = a * 10; a = 5;\n'
               ' printf("%d %d\\n", t, a); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)

    def test_read_through_a_pointer_is_not_delayed_past_a_write(self):
        res, orig, opt = run_both(self, '#include <stdio.h>\nint main() {\n'
            ' int a = 20, b = 4, c = 1, d = 4; double x = 2.0; int k = 7; int *p = &b;\n'
            ' d = ((k + *p) + (5 - c)) % 1000; b = x; x = d; d = 3 % 1000;\n'
            ' printf("%d %d %d %d %.4f\\n", a, b, c, d, x); return 0; }\n')
        self.assertEqual(orig, opt)
        self.assertIn("15.0000", orig[0])

    def test_dead_division_that_can_trap_is_kept(self):
        src = ('#include <stdio.h>\nint f(int a, int b) { int q = a / b; return a; }\n'
               'int main() { printf("%d\\n", f(10, 0)); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        # Whether int-division-by-zero actually traps (SIGFPE) is a CPU
        # property, not something this optimiser controls (e.g. it does
        # not trap on arm64, but does on x86). What must hold regardless
        # of the platform is that the original and optimised programs
        # behave *identically* -- already checked above -- and that the
        # division was not folded away as a dead store.
        self.assertIn("a / b", res["optimised_c"])

    def test_dead_assignment_from_a_call_still_makes_the_call(self):
        src = ('#include <stdio.h>\nint noisy() { printf("side effect\\n"); return 7; }\n'
               'int main() { int unused = noisy(); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertIn("side effect", orig[0])
        self.assertIn("noisy()", res["optimised_c"])

    def test_dead_stores_inside_loops_do_not_hide_calls_or_array_writes(self):
        res, orig, opt = run_both(self, program_text("loops_calls.c"))
        self.assertEqual(orig, opt)
        res, orig, opt = run_both(self, program_text("loops_arrays_pointers.c"))
        self.assertEqual(orig, opt)

    def test_every_claimed_variable_removal_is_visible_in_the_output(self):
        for name in ("opt_calculate_example.c", "opt_loops_baseline.c", "opt_self_assign.c"):
            res = optimised(program_text(name))
            c = res["optimised_c"]
            for ev in res["optimisation_report"]["applied"]:
                m = re.search(r"variable '(\w+)' was assigned once", ev["message"])
                if m:
                    with self.subTest(program=name, variable=m.group(1)):
                        self.assertNotRegex(c, rf"\b(?:int|double|char|float)\s+{m.group(1)}\b")

    def test_every_claimed_self_assignment_is_gone_from_the_output(self):
        res = optimised(program_text("opt_self_assign.c"))
        for ev in res["optimisation_report"]["applied"]:
            m = re.search(r"self-assignment '([^']+)' removed", ev["message"])
            if m:
                self.assertNotIn(m.group(1) + ";", res["optimised_c"])

    def test_zero_is_not_substituted_into_the_left_of_a_float_subtraction(self):
        # GCC turns `0.0 - x` into `-x`, which prints -0.0 where the original printed 0.0
        src = ('#include <stdio.h>\nint main() { double y = 0.0; int a = 0;\n'
               ' double s = y - (double)a; printf("%.4f\\n", s); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)


class ChainedConstantFolding(unittest.TestCase):
    """Constant folding/propagation must settle a chain of additions into
    a single constant, including across an intervening variable read, and
    must do so whether the chain appears straight-line or inside a loop."""

    def test_chained_additions_fold_to_one_constant(self):
        src = ('#include <stdio.h>\nint main() { int sum = 5;\n'
               ' sum = sum + 10 + 20;\n'
               ' printf("%d\\n", sum); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "35")
        self.assertIn("35", res["optimised_c"])
        self.assertNotIn("10", res["optimised_c"])

    def test_constant_propagates_across_a_branch_and_into_a_chain(self):
        src = ('#include <stdio.h>\nint main() { int a = 7; int b;\n'
               ' if (a > 0) { b = a + 3; } else { b = a - 3; }\n'
               ' int c = b + 1 + 1;\n'
               ' printf("%d\\n", c); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "12")
        self.assertIn("printf(\"%d\\n\", 12);", res["optimised_c"])

    def test_constant_propagates_through_a_loop_with_a_fixed_trip_count(self):
        src = ('#include <stdio.h>\nint main() { int total = 0;\n'
               ' for (int i = 0; i < 3; i++) { int step = 2 + 3; total = total + step; }\n'
               ' printf("%d\\n", total); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "15")
        self.assertIn("5", res["optimised_c"])


class PointerArraySimplification(unittest.TestCase):
    """`*(p + i)` and `*(p + i) = v` are exactly `p[i]` and `p[i] = v` per
    the C standard, so the optimiser rewrites the pointer-arithmetic idiom
    this compiler emits for them into ordinary array indexing, without
    changing what the program prints."""

    def test_pointer_plus_offset_read_becomes_indexing(self):
        src = ('#include <stdio.h>\nint main() {\n'
               ' int arr[5] = {1, 2, 3, 4, 5}; int *p = arr; int sum = 0;\n'
               ' for (int i = 0; i < 5; i++) { sum += *(p + i); }\n'
               ' printf("%d\\n", sum); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "15")
        self.assertIn("p[i]", res["optimised_c"])
        self.assertNotIn("*(p + i)", res["optimised_c"])

    def test_pointer_plus_offset_write_becomes_indexing(self):
        src = ('#include <stdio.h>\nint main() {\n'
               ' int arr[5] = {1, 2, 3, 4, 5}; int *p = arr;\n'
               ' for (int i = 0; i < 5; i++) { *(p + i) = i * 2; }\n'
               ' for (int i = 0; i < 5; i++) { printf("%d ", arr[i]); }\n'
               ' printf("\\n"); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "0 2 4 6 8")
        self.assertIn("p[i] = ", res["optimised_c"])

    def test_offset_plus_pointer_order_is_also_simplified(self):
        src = ('#include <stdio.h>\nint main() {\n'
               ' int arr[5] = {1, 2, 3, 4, 5}; int *p = arr; int i = 2;\n'
               ' int v = *(i + p);\n'
               ' printf("%d\\n", v); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "3")

    def test_pointer_used_twice_is_not_collapsed_into_two_index_expressions(self):
        # `t` is read by two different dereferences, so it must stay a
        # real variable; the simplification only fires for a single use.
        src = ('#include <stdio.h>\nint main() {\n'
               ' int arr[5] = {1, 2, 3, 4, 5}; int *p = arr;\n'
               ' int *t = p + 2; int v = *t + *t;\n'
               ' printf("%d\\n", v); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "6")


class RedundantCalculationElimination(unittest.TestCase):
    """A second loop (or a second call to a provably pure function) that
    recomputes a value the program already computed is proven equivalent
    and dropped, with the later uses reading the first result instead --
    `eliminate_redundant_loops` / the pure-call CSE extension in
    `common_subexpression_elimination`. Behaviour (stdout, exit code) must
    stay identical, and the duplicate work must actually be gone from the
    emitted C, not just left in place alongside a correct result."""

    def test_array_sum_recomputed_through_an_aliased_pointer_is_reused(self):
        res, orig, opt = run_both(self, program_text("opt_redundant_array_sum.c"))
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "Sum: 150 Total: 150")
        self.assertGreaterEqual(res["optimisation_stats"]["redundant_calculations_removed"], 1)
        c = res["optimised_c"]
        # only one loop should survive -- the second was proven to compute
        # exactly the same sum over the same array and removed outright
        self.assertEqual(c.count("for ("), 1)
        self.assertNotIn("ptr", c)

    def test_array_sum_recomputed_with_a_renamed_induction_variable_is_reused(self):
        # same proof, but nothing is shared textually between the loops --
        # different accumulator name, different induction variable, the
        # second loop counting down instead of up to the same bound -- so
        # this exercises the local-variable renaming in the equivalence
        # check, not just two loops that happen to look identical already.
        src = ('#include <stdio.h>\nint main() {\n'
               ' int arr[4] = {1, 2, 3, 4};\n'
               ' int acc = 0;\n'
               ' for (int i = 0; i < 4; i++) { acc = acc + arr[i]; }\n'
               ' int again = 0;\n'
               ' for (int j = 0; j < 4; j++) { again = again + arr[j]; }\n'
               ' printf("%d %d\\n", acc, again); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "10 10")
        self.assertGreaterEqual(res["optimisation_stats"]["redundant_calculations_removed"], 1)
        self.assertEqual(res["optimised_c"].count("for ("), 1)

    def test_unrelated_loops_are_left_alone(self):
        # a loop summing a *different* array must never be mistaken for a
        # repeat of the first -- the proof has to fail, not just happen
        # not to fire.
        src = ('#include <stdio.h>\nint main() {\n'
               ' int a[3] = {1, 2, 3}; int b[3] = {10, 20, 30};\n'
               ' int sa = 0; for (int i = 0; i < 3; i++) { sa = sa + a[i]; }\n'
               ' int sb = 0; for (int i = 0; i < 3; i++) { sb = sb + b[i]; }\n'
               ' printf("%d %d\\n", sa, sb); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "6 60")
        self.assertEqual(res["optimisation_stats"]["redundant_calculations_removed"], 0)
        self.assertEqual(res["optimised_c"].count("for ("), 2)

    def test_repeated_call_to_a_pure_function_is_reused(self):
        src = ('#include <stdio.h>\n'
               'int square(int n) { return n * n; }\n'
               'int main() { int x = 7;\n'
               ' printf("%d\\n", square(x) + square(x)); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "98")
        self.assertGreaterEqual(res["optimisation_stats"]["subexpressions_reused"], 1)
        # the call site itself must appear only once in the optimised main
        # (the function's own definition still names `square` once more) --
        # the second use reads the first call's result instead of calling again
        self.assertEqual(res["optimised_c"].count("square(x)") + res["optimised_c"].count("square(7)"), 1)

    def test_repeated_call_to_a_function_with_side_effects_is_kept(self):
        # a function that writes a global is not pure: a second call with
        # the same arguments could still see a different result, so both
        # calls must survive.
        src = ('#include <stdio.h>\n'
               'int counter = 0;\n'
               'int tick(int n) { counter = counter + 1; return n + counter; }\n'
               'int main() { printf("%d\\n", tick(10) + tick(10)); return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(res["optimised_c"].count("tick(10)"), 2)


class ChainedComparisonEmission(unittest.TestCase):
    """A relational/equality comparison used as the operand of another
    one at the same precedence (`(a <= b) >= c`) must come back out
    parenthesised: although left-associative chaining means the bare form
    `a <= b >= c` evaluates identically, Apple clang rejects it outright
    (chained-comparison diagnostic promoted to an error, even under -w),
    so losing the parentheses during emission would make the optimised
    program fail to compile at all."""

    def test_chained_relational_comparison_still_compiles(self):
        src = ('#include <stdio.h>\nint main() { int a = 1, b = 2, c = 3;\n'
               ' if ((a <= b) >= c) { printf("yes\\n"); } else { printf("no\\n"); }\n'
               ' return 0; }\n')
        res, orig, opt = run_both(self, src)
        self.assertEqual(orig, opt)
        self.assertEqual(orig[0].strip(), "no")


class RandomisedDifferential(unittest.TestCase):
    """A fixed-seed batch of randomly generated programs (see
    tests/fuzz_differential.py, which can run thousands more)."""

    def test_random_programs_behave_identically(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import fuzz_differential
        failures, _ = fuzz_differential.fuzz(40, seed=2024, save=False, verbose=False)
        self.assertEqual([(i, st) for i, st, _, _ in failures], [])


if __name__ == "__main__":
    unittest.main()
