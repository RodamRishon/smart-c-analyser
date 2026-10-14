"""
Command-line driver
--------------------
Runs the full pipeline -- lexical analysis, syntax analysis, semantic
analysis, IR generation, optimization -- over a C source file and
prints a structured report for each stage, matching the "Expected
Outcome" of the project proposal:

    (a) a token stream with any lexical errors identified
    (b) confirmation of successful parsing, or syntax errors with line numbers
    (c) a semantic-analysis report (undeclared vars, type mismatches,
        scope errors) together with the symbol table
    (d) an optimisation report (dead-code removal, constant folding,
        common subexpression elimination) and the resulting optimised
        intermediate code

If lexical or syntax errors are found, later stages are skipped,
the same way a real compiler won't try to generate code for a
program that doesn't parse.
"""

import os
import sys
import argparse

from .lexer import Lexer
from .parser import Parser
from .semantic import SemanticAnalyzer
from .ir import IRGenerator, Instr
from .optimizer import optimize_program, _display, EVENT_LABELS, UNSUPPORTED_OPTIMISATIONS
from .emitter import emit_program
from .structurer import control_flow_summary
from .preprocessor import strip_directives


RULE = "-" * 70


def _section(title: str):
    print()
    print(RULE)
    print(title)
    print(RULE)


def _print_tokens(tokens, limit=None):
    shown = tokens[:-1]  # drop EOF for display
    if limit:
        shown = shown[:limit]
    for t in shown:
        print(f"  line {t.line:>4}  {t.kind:<12} {t.value!r}")
    if limit and len(tokens) - 1 > limit:
        print(f"  ... {len(tokens) - 1 - limit} more token(s)")


def _print_symbol_table(symbols):
    if not symbols:
        print("  (no symbols declared)")
        return
    header = f"  {'name':<15}{'kind':<10}{'type':<16}{'scope':<8}{'line':<6}{'extra'}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for s in symbols:
        extra = ""
        if s.kind == "func":
            extra = f"params=({', '.join(s.params or [])})"
        elif s.is_array:
            dims = s.array_dims or [s.array_size]
            extra = "size=" + "".join(f"[{d}]" for d in dims)
        print(f"  {s.name:<15}{s.kind:<10}{s.type:<16}{s.scope_level:<8}{s.line:<6}{extra}")


def _print_ir(global_instrs, functions):
    if global_instrs:
        print("  [globals]")
        for instr in global_instrs:
            print(" ", instr)
        print()
    for fn in functions:
        print(f"  [function {fn.name}({', '.join(fn.params)}) -> {fn.return_type}]")
        if not fn.instructions:
            print("    (empty)")
        for instr in fn.instructions:
            print(" ", instr)
        print()


def run(path: str, out_path=None, show_tokens=60) -> int:
    try:
        with open(path, "r") as f:
            source = f.read()
    except OSError as e:
        print(f"error: cannot read '{path}': {e}")
        return 1

    print(f"Smart C Code Analyser and Optimiser")
    print(f"Input file: {path}")

    # -- (0) preprocessing ---------------------------------------------------
    _section("PREPROCESSING")
    base_dir = os.path.dirname(os.path.abspath(path))
    source, pp_errors = strip_directives(source, base_dir=base_dir)
    if pp_errors:
        print(f"{len(pp_errors)} preprocessor error(s):")
        for e in pp_errors:
            print(" ", e)
        print("\nStopping: fix preprocessor errors before continuing.")
        return 1
    print("No preprocessor errors found.")

    # -- (a) lexical analysis -------------------------------------------------
    _section("LEXICAL ANALYSIS")
    lexer = Lexer(source)
    tokens = lexer.tokenize()
    print(f"Tokens produced: {len(tokens) - 1}")
    _print_tokens(tokens, limit=show_tokens or None)

    if lexer.errors:
        print(f"\n{len(lexer.errors)} lexical error(s):")
        for e in lexer.errors:
            print(" ", e)
        print("\nStopping: fix lexical errors before continuing.")
        return 1
    print("\nNo lexical errors found.")

    # -- (b) syntax analysis -------------------------------------------------
    _section("SYNTAX ANALYSIS")
    parser = Parser(tokens)
    program = parser.parse()

    if parser.errors:
        print(f"{len(parser.errors)} syntax error(s):")
        for e in parser.errors:
            print(" ", e)
        print("\nStopping: fix syntax errors before continuing.")
        return 1
    print("Parsing successful -- no syntax errors.")
    print(f"Top-level declarations: {len(program.declarations)}")

    # -- (c) semantic analysis ------------------------------------------------
    _section("SEMANTIC ANALYSIS")
    analyzer = SemanticAnalyzer()
    diagnostics = analyzer.analyze(program)

    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]

    if diagnostics:
        for d in diagnostics:
            print(" ", d)
    else:
        print("No semantic issues found.")

    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s).")

    print("\nSymbol table:")
    _print_symbol_table(analyzer.all_symbols)

    if errors:
        print("\nStopping: fix semantic errors before optimisation can run.")
        return 1

    # -- IR generation ----------------------------------------------------
    _section("INTERMEDIATE CODE (before optimisation)")
    ir_gen = IRGenerator()
    global_instrs, functions = ir_gen.generate(program)
    _print_ir(global_instrs, functions)

    # -- (d) optimisation --------------------------------------------------
    _section("OPTIMISATION")
    total_before = len(global_instrs) + sum(len(fn.instructions) for fn in functions)

    global_instrs_opt, all_stats = optimize_program(global_instrs, functions)

    total_folded = sum(s.constants_folded for _, s in all_stats)
    total_algebraic = sum(s.algebraic_simplifications for _, s in all_stats)
    total_reused = sum(s.subexpressions_reused for _, s in all_stats)
    total_dead = sum(s.dead_stores_removed for _, s in all_stats)
    total_unreachable = sum(s.unreachable_instructions_removed for _, s in all_stats)
    total_copies = sum(s.copies_propagated for _, s in all_stats)
    total_vars = sum(s.variables_eliminated for _, s in all_stats)
    total_pointer = sum(s.pointer_simplifications for _, s in all_stats)
    total_after = len(global_instrs_opt) + sum(len(fn.instructions) for fn in functions)
    passes_run = max((s.passes_run for _, s in all_stats), default=0)

    print("APPLIED -- changes actually made to the program:")
    any_events = False
    for fname, stats in all_stats:
        for ev in stats.events:
            any_events = True
            where = "" if fname == "<globals>" else f" [{fname}]"
            print(f"  {EVENT_LABELS.get(ev.kind, ev.kind)}{where}: line {ev.line}: {ev.message}")
    if not any_events:
        print("  (none -- no applicable optimisations were found)")

    print("\nDETECTED BUT NOT APPLIED -- opportunities found and deliberately left alone:")
    any_skipped = False
    for fname, stats in all_stats:
        for ev in stats.skipped:
            any_skipped = True
            where = "" if fname == "<globals>" else f" [{fname}]"
            print(f"  {EVENT_LABELS.get(ev.kind, ev.kind)}{where}: line {ev.line}: {ev.message}")
    if not any_skipped:
        print("  (none)")

    print("\nNOT SUPPORTED BY THIS TOOL (never attempted):")
    for name in UNSUPPORTED_OPTIMISATIONS:
        print(f"  - {name}")
    if not all(st.converged for _, st in all_stats):
        print("\nNote: the pass loop hit its iteration cap before settling; "
              "the output is still correct but may not be fully optimised.")

    print()
    print(f"Optimisation passes performed:      {passes_run}")
    print(f"Constant expressions folded:        {total_folded}")
    print(f"Algebraic simplifications:          {total_algebraic}")
    print(f"Common subexpressions reused:       {total_reused}")
    print(f"Dead stores eliminated:             {total_dead}")
    print(f"Unreachable instructions removed:   {total_unreachable}")
    print(f"Copies propagated:                  {total_copies}")
    print(f"Single-use variables eliminated:    {total_vars}")
    print(f"Pointer expressions simplified:     {total_pointer}")
    print(f"\nInstruction count: {total_before} -> {total_after} "
          f"({total_before - total_after} removed)")

    _section("INTERMEDIATE CODE (after optimisation)")
    _print_ir(global_instrs_opt, functions)

    _section("OPTIMISED C CODE")
    optimised_c = emit_program(global_instrs_opt, functions, program)
    print(optimised_c)
    flow = control_flow_summary(optimised_c)
    print(f"Control flow in the output: {flow['loops']} loop(s), {flow['ifs']} if statement(s), "
          f"{flow['gotos']} goto(s)")
    original_lines = len(source.splitlines())
    optimised_lines = len(optimised_c.splitlines())
    print(f"Line count: {original_lines} (original) -> {optimised_lines} (optimised)")
    if out_path:
        with open(out_path, "w") as f:
            f.write(optimised_c)
        print(f"(written to {out_path})")

    if warnings:
        print(f"(Completed with {len(warnings)} warning(s) -- see SEMANTIC ANALYSIS above.)")

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="smartc",
        description="Smart C Code Analyser and Optimiser",
    )
    parser.add_argument("file", help="path to the C source file to analyse")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="also write the optimised C code to FILE")
    parser.add_argument("--all-tokens", action="store_true",
                        help="print the whole token stream instead of the first 60")
    args = parser.parse_args(argv)
    return run(args.file, out_path=args.output,
               show_tokens=None if args.all_tokens else 60)


if __name__ == "__main__":
    sys.exit(main())
