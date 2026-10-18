"""
Structured pipeline runner
--------------------------
Runs the same lexer -> parser -> semantic -> IR -> optimizer -> emitter
pipeline as cli.py, but returns a plain dict of structured results
instead of printing a report. Used by both the Flask web app and
(optionally) anything else that wants programmatic access to a run.
"""

from .lexer import Lexer
from .parser import Parser
from .semantic import SemanticAnalyzer
from .ir import IRGenerator
from .optimizer import optimize_program, EVENT_LABELS, UNSUPPORTED_OPTIMISATIONS
from .emitter import emit_program
from .preprocessor import strip_directives
from .structurer import control_flow_summary


def _token_rows(tokens):
    return [{"line": t.line, "kind": t.kind, "value": t.value} for t in tokens[:-1]]


def _symbol_rows(symbols):
    rows = []
    for s in symbols:
        extra = ""
        if s.kind == "func":
            extra = f"params=({', '.join(s.params or [])})"
        elif s.is_array:
            dims = s.array_dims or [s.array_size]
            extra = "size=" + "".join(f"[{d}]" for d in dims)
        rows.append({
            "name": s.name, "kind": s.kind, "type": s.type,
            "scope": s.scope_level, "line": s.line, "extra": extra,
        })
    return rows


def _ir_text(global_instrs, functions):
    lines = []
    if global_instrs:
        lines.append("[globals]")
        for instr in global_instrs:
            lines.append(f"  {instr}")
        lines.append("")
    for fn in functions:
        lines.append(f"[function {fn.name}({', '.join(fn.params)}) -> {fn.return_type}]")
        if not fn.instructions:
            lines.append("  (empty)")
        for instr in fn.instructions:
            lines.append(f"  {instr}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def analyze(source: str) -> dict:
    """Run the full pipeline over a C source string.

    Returns a dict with keys for every stage; later stages are omitted
    once an earlier stage reports errors, mirroring the CLI's
    stop-on-error behaviour.
    """
    result = {
        "stage_reached": "preprocessing",
        "preprocessor_errors": [],
        "tokens": [],
        "lexical_errors": [],
        "syntax_errors": [],
        "top_level_decls": 0,
        "diagnostics": [],
        "symbols": [],
        "ir_before": "",
        "optimisation_events": [],
        "optimisation_stats": {},
        "ir_after": "",
        "optimised_c": "",
        "success": False,
    }

    source, pp_errors = strip_directives(source)
    result["preprocessor_errors"] = [str(e) for e in pp_errors]
    if pp_errors:
        return result

    result["stage_reached"] = "lexical"
    lexer = Lexer(source)
    tokens = lexer.tokenize()
    result["tokens"] = _token_rows(tokens)
    result["lexical_errors"] = [str(e) for e in lexer.errors]
    if lexer.errors:
        return result

    result["stage_reached"] = "syntax"
    parser = Parser(tokens)
    program = parser.parse()
    result["syntax_errors"] = [str(e) for e in parser.errors]
    if parser.errors:
        return result
    result["top_level_decls"] = len(program.declarations)

    result["stage_reached"] = "semantic"
    analyzer = SemanticAnalyzer()
    diagnostics = analyzer.analyze(program)
    result["diagnostics"] = [
        {"severity": d.severity, "text": str(d)} for d in diagnostics
    ]
    result["symbols"] = _symbol_rows(analyzer.all_symbols)
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    result["error_count"] = len(errors)
    result["warning_count"] = len(warnings)
    if errors:
        return result

    result["stage_reached"] = "ir"
    ir_gen = IRGenerator()
    global_instrs, functions = ir_gen.generate(program)
    result["ir_before"] = _ir_text(global_instrs, functions)

    result["stage_reached"] = "optimisation"
    total_before = len(global_instrs) + sum(len(fn.instructions) for fn in functions)

    global_instrs_opt, all_stats = optimize_program(global_instrs, functions)

    def rows(attr):
        out = []
        for fname, stats in all_stats:
            for ev in getattr(stats, attr):
                where = "" if fname == "<globals>" else f" [{fname}]"
                out.append({
                    "label": EVENT_LABELS.get(ev.kind, ev.kind) + where,
                    "line": ev.line,
                    "message": ev.message,
                })
        return out

    events = rows("events")          # applied
    detected = rows("skipped")       # found, deliberately not applied
    result["optimisation_events"] = events
    result["optimisation_report"] = {
        "applied": events,
        "detected_not_applied": detected,
        "unsupported": list(UNSUPPORTED_OPTIMISATIONS),
    }

    total_after = len(global_instrs_opt) + sum(len(fn.instructions) for fn in functions)
    passes_run = max((s.passes_run for _, s in all_stats), default=0)
    result["optimisation_stats"] = {
        "passes_run": passes_run,
        "constants_folded": sum(s.constants_folded for _, s in all_stats),
        "algebraic_simplifications": sum(s.algebraic_simplifications for _, s in all_stats),
        "subexpressions_reused": sum(s.subexpressions_reused for _, s in all_stats),
        "dead_stores_removed": sum(s.dead_stores_removed for _, s in all_stats),
        "unreachable_removed": sum(s.unreachable_instructions_removed for _, s in all_stats),
        "copies_propagated": sum(s.copies_propagated for _, s in all_stats),
        "variables_eliminated": sum(s.variables_eliminated for _, s in all_stats),
        "pointer_simplifications": sum(s.pointer_simplifications for _, s in all_stats),
        "redundant_calculations_removed": sum(s.redundant_calculations_removed for _, s in all_stats),
        "converged": all(st.converged for _, st in all_stats),
        "instructions_before": total_before,
        "instructions_after": total_after,
    }
    result["no_optimisations_found"] = not events

    result["ir_after"] = _ir_text(global_instrs_opt, functions)

    result["stage_reached"] = "done"
    optimised_c = emit_program(global_instrs_opt, functions, program)
    result["optimised_c"] = optimised_c
    result["control_flow"] = control_flow_summary(optimised_c)
    result["line_counts"] = {
        "original": len(source.splitlines()),
        "optimised": len(optimised_c.splitlines()),
    }
    result["success"] = True
    return result
