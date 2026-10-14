"""
Randomised differential tester for the optimiser.

Generates small C programs that have *defined* behaviour (bounded values,
no division by zero, in-range array indices, no signed overflow), mixing
int / double / char, shadowed locals, nested blocks, loops, pointers,
arrays, global state, function calls and side-effecting calls inside
`&&` / `||`. Each program is compiled twice with the system C compiler --
once as generated, once as the optimiser's emitted C -- and stdout plus
the exit status must be identical.

    python3 tests/fuzz_differential.py            # 200 programs, seed 1
    python3 tests/fuzz_differential.py 500 7      # 500 programs, seed 7

A failing program is saved to tests/fuzz_failures/ for inspection.
The unit-test suite runs a small fixed-seed batch (see test_smartc.py).
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from smartc.pipeline import analyze  # noqa: E402

CC = shutil.which("cc") or shutil.which("gcc")


class Gen:
    def __init__(self, rng):
        self.r = rng
        self.int_vars = ["a", "b", "c", "d"]
        self.dbl_vars = ["x", "y"]
        self.indent = 1
        self.lines = []
        self.depth = 0
        self.fresh = 0
        self.in_loop = 0

    # -- expressions ----------------------------------------------------
    def ie(self, d=2):
        """int expression, |value| well below 2^31."""
        r = self.r
        if d <= 0 or r.random() < 0.3:
            return r.choice(self.int_vars + [str(r.randint(0, 40)), str(r.randint(0, 9)),
                                             "arr[%s]" % self.idx(), "*p", "g", "k"])
        k = r.random()
        if k < 0.30:
            return f"({self.ie(d-1)} + {self.ie(d-1)})"
        if k < 0.50:
            return f"({self.ie(d-1)} - {self.ie(d-1)})"
        if k < 0.62:
            return f"(({self.ie(d-1)} % 31) * ({self.ie(d-1)} % 31))"
        if k < 0.70:
            return f"({self.ie(d-1)} / ({self.ie(d-1)} % 7 + 8))"
        if k < 0.76:
            return f"({self.ie(d-1)} % ({self.ie(d-1)} % 7 + 8))"
        if k < 0.82:
            return f"({self.cond(d-1)})"
        if k < 0.88:
            return f"(-{self.ie(d-1)})"
        if k < 0.93:
            return f"({self.ie(d-1)} * 1)" if r.random() < 0.5 else f"({self.ie(d-1)} + 0)"
        if k < 0.97:
            return "(int)ch"
        return f"{self.ie(d-1)}"

    def de(self, d=2):
        r = self.r
        if d <= 0 or r.random() < 0.3:
            return r.choice(self.dbl_vars + ["1.5", "0.25", "2.0", "0.0", "3.75", "gd", "(double)a"])
        k = r.random()
        if k < 0.30:
            return f"({self.de(d-1)} + {self.de(d-1)})"
        if k < 0.50:
            return f"({self.de(d-1)} - {self.de(d-1)})"
        if k < 0.65:
            return f"({self.de(d-1)} * {self.de(d-1)})"
        if k < 0.72:
            return f"({self.de(d-1)} / ({self.de(d-1)} * {self.de(d-1)} + 1.0)"  + ")"
        if k < 0.80:
            return f"({self.de(d-1)} * 0)" if r.random() < 0.5 else f"({self.de(d-1)} + 0)"
        if k < 0.88:
            return f"({self.de(d-1)} / 2)"
        if k < 0.94:
            return f"(a / 2 + {self.de(d-1)})"
        return f"(-{self.de(d-1)})"

    def idx(self):
        return f"(({self.r.choice(self.int_vars)} % 4 + 4) % 4)"

    def cond(self, d=2):
        r = self.r
        k = r.random()
        cmp_ = r.choice(["<", ">", "<=", ">=", "==", "!="])
        if k < 0.35:
            return f"{self.ie(d)} {cmp_} {self.ie(d)}"
        if k < 0.50:
            return f"{self.de(d)} {cmp_} {self.de(d)}"
        if k < 0.65:
            return f"{self.cond(d-1)} && {self.cond(d-1)}" if d > 0 else "a < b"
        if k < 0.78:
            return f"{self.cond(d-1)} || {self.cond(d-1)}" if d > 0 else "b < c"
        if k < 0.88:
            # side-effecting call guarded by a short-circuit operand
            op = r.choice(["&&", "||"])
            return f"({self.cond(0)} {op} bump({r.randint(1, 3)}) > {r.randint(0, 2)})"
        if k < 0.94:
            return f"!({self.cond(d-1)})" if d > 0 else "!(a == b)"
        return f"(*p > 0 && arr[{self.idx()}] > {r.randint(0, 20)})"

    # -- statements -----------------------------------------------------
    def emit(self, s):
        self.lines.append("    " * self.indent + s)

    def clamp_d(self, v):
        self.emit(f"if ({v} > 1000000.0 || {v} < -1000000.0) {v} = 1.5;")

    def stmt(self):
        r = self.r
        if self.in_loop and r.random() < 0.12:
            # control transfer out of / within a loop; guarded so the loop
            # still runs a few times
            word = r.choice(["break;", "continue;", "break;", "continue;", "return a % 7;"])
            self.emit(f"if ({self.cond(1)}) {{ {word} }}")
            return
        k = r.random()
        v = r.choice(["a", "b", "c", "d"])      # loop counters are read-only
        if k < 0.22:
            self.emit(f"{v} = {self.ie()} % 1000;")
        elif k < 0.30:
            self.emit(f"{v} += {self.ie(1)} % 50;")
        elif k < 0.38:
            dv = r.choice(self.dbl_vars)
            self.emit(f"{dv} = {self.de()};")
            self.clamp_d(dv)
        elif k < 0.43:
            dv = r.choice(self.dbl_vars)
            self.emit(f"{v} = {dv};")                 # double -> int (value is clamped)
        elif k < 0.47:
            dv = r.choice(self.dbl_vars)
            self.emit(f"{dv} = {v};")                 # int -> double
        elif k < 0.485:
            # floating-point identities that are NOT exact: x*0 is -0.0 for
            # negative x, x+0 turns -0.0 into +0.0
            dv, dw = r.choice(self.dbl_vars), r.choice(self.dbl_vars)
            self.emit(f"{dv} = {dw} * 0;" if r.random() < 0.5 else f"{dv} = (0 - {dw}) * 0;")
            self.emit(f"{dw} = {dv} + 0;")
            self.emit(f"printf(\"%.4f %.4f\\n\", {dv}, {dw});")
        elif k < 0.51:
            self.emit(f"ch = {self.ie(1)};")           # int -> char narrowing
        elif k < 0.55:
            self.emit(f"*p = {self.ie(1)} % 100;")
        elif k < 0.60:
            self.emit(f"arr[{self.idx()}] = {self.ie(1)} % 100;")
        elif k < 0.64:
            self.emit(f"{v} = f1({self.ie(1)} % 50, {self.ie(1)} % 50);")
        elif k < 0.67:
            self.emit(f"g = {self.ie(1)} % 100;")
        elif k < 0.69:
            self.emit(f"{v}++;")
        elif k < 0.72:
            self.emit(f"printf(\"%d %d %.4f\\n\", {self.ie(1)}, {v}, {self.de(1)});")
        elif k < 0.80 and self.depth < 2:
            self.emit(f"if ({self.cond()}) {{")
            self.block()
            if r.random() < 0.5:
                self.emit("} else {")
                self.block()
            self.emit("}")
        elif k < 0.86 and self.depth < 2:
            i = f"i{self.fresh}"
            self.fresh += 1
            self.emit(f"for (int {i} = 0; {i} < {r.randint(1, 4)}; {i}++) {{")
            self.int_vars.append(i)
            self.in_loop += 1
            self.block()
            self.in_loop -= 1
            self.int_vars.remove(i)
            self.emit("}")
        elif k < 0.90 and self.depth < 2:
            w = f"w{self.fresh}"
            self.fresh += 1
            self.emit(f"int {w} = 0;")
            self.emit(f"while ({w} < {r.randint(1, 3)} && {self.cond(1)}) {{")
            self.indent += 1
            self.emit(f"{w}++;")
            self.indent -= 1
            self.in_loop += 1
            self.block()
            self.in_loop -= 1
            self.emit("}")
        elif k < 0.95 and self.depth < 2:
            # shadowing in a nested block. The initialiser is computed *outside*
            # first: `int a = a % 100;` would read the new, uninitialised `a`.
            shadow = r.choice(["a", "b", "x"])
            tmp = f"s{self.fresh}"
            self.fresh += 1
            if shadow == "x":
                self.emit(f"double {tmp} = {self.de(1)};")
                self.clamp_d(tmp)
            else:
                self.emit(f"int {tmp} = {self.ie(1)} % 100;")
            self.emit("{")
            self.indent += 1
            if shadow == "x":
                self.emit(f"double x = {tmp};")
                self.emit("printf(\"in %.4f\\n\", x);")
            else:
                self.emit(f"int {shadow} = {tmp};")
                self.emit(f"printf(\"in %d\\n\", {shadow});")
            self.indent -= 1
            self.depth += 1
            self.indent += 1
            for _ in range(r.randint(1, 3)):
                self.stmt()
            self.indent -= 1
            self.depth -= 1
            self.emit("}")
        else:
            self.emit(f"{v} = {self.ie()} % 1000;")

    def block(self):
        self.depth += 1
        self.indent += 1
        for _ in range(self.r.randint(1, 4)):
            self.stmt()
        self.indent -= 1
        self.depth -= 1

    def program(self):
        r = self.r
        head = [
            "#include <stdio.h>",
            "int g = %d;" % r.randint(0, 9),
            "double gd = %s;" % r.choice(["0.5", "2.0", "1.25"]),
            "int h = 0;",
            "int bump(int n) { h = h + n; return n % 3 + 1; }",
            "int f1(int p, int q) { int t = p * 2 + q; if (t > 40) { t = t - 40; } return t; }",
            "int main() {",
        ]
        self.emit(f"int a = {r.randint(0, 20)}, b = {r.randint(0, 20)};")
        self.emit(f"int c = {r.randint(0, 20)};")
        self.emit(f"int d = {r.randint(0, 20)} + {r.randint(0, 20)};")
        self.emit(f"double x = {r.choice(['1.5', '2.0', '0.5'])};")
        self.emit("double y = 3 / 2;")
        self.emit(f"char ch = {r.randint(0, 100)};")
        self.emit("int k = 7;")
        self.emit("int arr[4] = {1, 2, 3, 4};")
        self.emit("int *p = &b;")
        for _ in range(r.randint(6, 16)):
            self.stmt()
        self.emit("printf(\"%d %d %d %d %.4f %.4f %d %d %d %d\\n\", a, b, c, d, x, y, (int)ch, g, k, h);")
        self.emit("printf(\"%d %d %d %d %d\\n\", arr[0], arr[1], arr[2], arr[3], *p);")
        self.emit("return (a + b + c + d) % 100;")
        return "\n".join(head + self.lines + ["}"]) + "\n"


def run_one(src, workdir):
    res = analyze(src)
    if not res.get("success"):
        return "REJECT", res
    p0 = os.path.join(workdir, "orig.c")
    p1 = os.path.join(workdir, "opt.c")
    with open(p0, "w") as f:
        f.write(src)
    with open(p1, "w") as f:
        f.write(res["optimised_c"])
    outs = []
    for tag, path in (("orig", p0), ("opt", p1)):
        exe = os.path.join(workdir, tag)
        flags = ["-fsanitize=undefined", "-fno-sanitize-recover=undefined"] if tag == "orig" else []
        r = subprocess.run([CC, "-w", "-O0", *flags, "-o", exe, path], capture_output=True, text=True)
        if r.returncode != 0:
            return ("ORIG-NOCOMPILE" if tag == "orig" else "OPT-NOCOMPILE"), r.stderr[:600]
        try:
            q = subprocess.run([exe], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return ("GEN-TIMEOUT" if tag == "orig" else "OPT-TIMEOUT"), tag
        if tag == "orig" and "runtime error" in q.stderr:
            return "GEN-UB", q.stderr[:200]       # the generated program itself is undefined
        outs.append((q.stdout, q.returncode))
    if outs[0] != outs[1]:
        return "DIFFERENT", outs
    return "SAME", None


def fuzz(n=200, seed=1, save=True, verbose=True):
    failures = []
    rejects = 0
    for i in range(n):
        rng = random.Random(seed * 100003 + i)
        src = Gen(rng).program()
        with tempfile.TemporaryDirectory() as d:
            status, info = run_one(src, d)
        if status in ("SAME", "GEN-TIMEOUT", "GEN-UB"):
            continue
        if status == "REJECT":
            rejects += 1
            if verbose and rejects <= 3:
                print(f"[{i}] rejected by the front end: "
                      f"{(info.get('syntax_errors') or info.get('diagnostics') or info.get('lexical_errors'))[:2]}")
            continue
        failures.append((i, status, info, src))
        if save:
            os.makedirs(os.path.join(ROOT, "tests", "fuzz_failures"), exist_ok=True)
            with open(os.path.join(ROOT, "tests", "fuzz_failures", f"seed{seed}_{i}.c"), "w") as f:
                f.write(src)
        if verbose:
            print(f"[{i}] {status}: {str(info)[:300]}")
    if verbose:
        print(f"{n} programs, {len(failures)} behaviour differences, {rejects} rejected by the front end")
    return failures, rejects


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    failed, _ = fuzz(n, seed)
    sys.exit(1 if failed else 0)
