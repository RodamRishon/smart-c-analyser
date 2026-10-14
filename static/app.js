const sourceEl = document.getElementById("source");
const resultsEl = document.getElementById("results");
const statusEl = document.getElementById("status");
const fileInput = document.getElementById("file-input");

const SAMPLE = sourceEl.value;

document.getElementById("sample-btn").addEventListener("click", () => {
  sourceEl.value = SAMPLE;
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { sourceEl.value = reader.result; };
  reader.readAsText(file);
});

document.getElementById("analyze-btn").addEventListener("click", runAnalysis);

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function card(title, badgeHtml, bodyHtml) {
  return `<div class="card"><h2>${title} ${badgeHtml || ""}</h2>${bodyHtml}</div>`;
}

function badge(text, cls) {
  return `<span class="badge ${cls || ""}">${esc(text)}</span>`;
}

async function runAnalysis() {
  const source = sourceEl.value;
  statusEl.className = "status";
  statusEl.textContent = "Analysing...";
  resultsEl.innerHTML = "";

  let resp, data;
  try {
    resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
    });
    data = await resp.json();
  } catch (e) {
    statusEl.className = "status error";
    statusEl.textContent = "Request failed: " + e;
    return;
  }

  if (!resp.ok) {
    statusEl.className = "status error";
    statusEl.textContent = data.error || "Analysis failed.";
    return;
  }

  render(data);
}

function render(data) {
  const parts = [];

  // Preprocessing
  if (data.preprocessor_errors && data.preprocessor_errors.length) {
    const ppBody = data.preprocessor_errors.map(e => `<div class="diag-line error">${esc(e)}</div>`).join("");
    parts.push(card("Preprocessing", badge(`${data.preprocessor_errors.length} error(s)`, "error"), ppBody));
    resultsEl.innerHTML = parts.join("");
    statusEl.className = "status error";
    statusEl.textContent = "Stopped: fix preprocessor errors before continuing.";
    return;
  }

  // Lexical
  const lexBadge = data.lexical_errors.length
    ? badge(`${data.lexical_errors.length} error(s)`, "error")
    : badge("ok", "ok");
  let lexBody = `<p class="empty">${data.tokens.length} token(s) produced.</p>`;
  if (data.lexical_errors.length) {
    lexBody += data.lexical_errors.map(e => `<div class="diag-line error">${esc(e)}</div>`).join("");
  } else {
    lexBody += "<pre>" + data.tokens.slice(0, 80).map(
      t => `line ${String(t.line).padStart(4)}  ${t.kind.padEnd(12)} ${JSON.stringify(t.value)}`
    ).join("\n") + (data.tokens.length > 80 ? `\n... ${data.tokens.length - 80} more` : "") + "</pre>";
  }
  parts.push(card("Lexical Analysis", lexBadge, lexBody));

  if (data.lexical_errors.length) {
    statusEl.className = "status error";
    statusEl.textContent = "Stopped: fix lexical errors before continuing.";
    resultsEl.innerHTML = parts.join("");
    return;
  }

  // Syntax
  const synBadge = data.syntax_errors.length
    ? badge(`${data.syntax_errors.length} error(s)`, "error")
    : badge("ok", "ok");
  let synBody = data.syntax_errors.length
    ? data.syntax_errors.map(e => `<div class="diag-line error">${esc(e)}</div>`).join("")
    : `<p class="empty">Parsing successful. ${data.top_level_decls} top-level declaration(s).</p>`;
  parts.push(card("Syntax Analysis", synBadge, synBody));

  if (data.syntax_errors.length) {
    statusEl.className = "status error";
    statusEl.textContent = "Stopped: fix syntax errors before continuing.";
    resultsEl.innerHTML = parts.join("");
    return;
  }

  // Semantic
  const semBadge = data.error_count
    ? badge(`${data.error_count} error(s)`, "error")
    : (data.warning_count ? badge(`${data.warning_count} warning(s)`, "warning") : badge("ok", "ok"));
  let semBody = data.diagnostics.length
    ? data.diagnostics.map(d => `<div class="diag-line ${d.severity}">${esc(d.text)}</div>`).join("")
    : `<p class="empty">No semantic issues found.</p>`;
  semBody += "<table><thead><tr><th>name</th><th>kind</th><th>type</th><th>scope</th><th>line</th><th>extra</th></tr></thead><tbody>" +
    data.symbols.map(s => `<tr><td>${esc(s.name)}</td><td>${esc(s.kind)}</td><td>${esc(s.type)}</td><td>${s.scope}</td><td>${s.line}</td><td>${esc(s.extra)}</td></tr>`).join("") +
    "</tbody></table>";
  parts.push(card("Semantic Analysis", semBadge, semBody));

  if (data.error_count) {
    statusEl.className = "status error";
    statusEl.textContent = "Stopped: fix semantic errors before optimisation can run.";
    resultsEl.innerHTML = parts.join("");
    return;
  }

  // IR before
  parts.push(card("Intermediate Code (before optimisation)", "", `<pre>${esc(data.ir_before)}</pre>`));

  // Optimisation
  const stats = data.optimisation_stats;
  const report = data.optimisation_report || {
    applied: data.optimisation_events, detected_not_applied: [], unsupported: [],
  };
  const eventLines = list => list.map(e =>
    `<div class="diag-line">${esc(e.label)}: line ${e.line}: ${esc(e.message)}</div>`).join("");
  let optBody = `<h3 class="subhead">Applied <span class="hint">changes actually made to the program</span></h3>`;
  optBody += report.applied.length
    ? eventLines(report.applied)
    : `<p class="empty">No applicable optimisations found.</p>`;
  optBody += `<h3 class="subhead">Detected but not applied <span class="hint">found, then deliberately left alone because applying them could change behaviour</span></h3>`;
  optBody += report.detected_not_applied.length
    ? eventLines(report.detected_not_applied)
    : `<p class="empty">None.</p>`;
  optBody += `<h3 class="subhead">Not supported by this tool <span class="hint">never attempted</span></h3>`;
  optBody += report.unsupported.length
    ? `<ul class="unsupported">${report.unsupported.map(u => `<li>${esc(u)}</li>`).join("")}</ul>`
    : `<p class="empty">None listed.</p>`;
  if (stats.converged === false) {
    optBody += `<p class="empty">The pass loop hit its iteration cap before settling; the output is still correct but may not be fully optimised.</p>`;
  }
  optBody += `<div class="stats-grid">
    <div><span class="n">${stats.passes_run}</span>passes performed</div>
    <div><span class="n">${stats.constants_folded}</span>constants folded</div>
    <div><span class="n">${stats.algebraic_simplifications}</span>algebraic simplifications</div>
    <div><span class="n">${stats.subexpressions_reused}</span>subexpressions reused</div>
    <div><span class="n">${stats.dead_stores_removed}</span>dead stores removed</div>
    <div><span class="n">${stats.unreachable_removed}</span>unreachable removed</div>
    <div><span class="n">${stats.copies_propagated}</span>copies propagated</div>
    <div><span class="n">${stats.pointer_simplifications}</span>pointer expressions simplified</div>
    <div><span class="n">${stats.instructions_before} &rarr; ${stats.instructions_after}</span>instruction count</div>
    <div><span class="n">${data.line_counts.original} &rarr; ${data.line_counts.optimised}</span>line count</div>
  </div>`;
  parts.push(card("Optimisation", "", optBody));

  // IR after
  parts.push(card("Intermediate Code (after optimisation)", "", `<pre>${esc(data.ir_after)}</pre>`));

  // Optimised C
  parts.push(card("Optimised C Code", "", `<pre>${esc(data.optimised_c)}</pre>`));

  resultsEl.innerHTML = parts.join("");
  statusEl.className = "status ok";
  statusEl.textContent = data.warning_count
    ? `Completed with ${data.warning_count} warning(s).`
    : "Completed successfully.";
}
