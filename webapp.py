"""
Smart C Code Analyser and Optimiser -- web app
-----------------------------------------------
A thin Flask front end over smartc.pipeline.analyze: paste or upload a
C source file in the browser, run it through the same lexer -> parser
-> semantic -> IR -> optimizer pipeline as analyse.py, and see every
stage's output rendered in the page.

Run:
    python3 webapp.py
then open http://127.0.0.1:5000 in a browser.
"""

from flask import Flask, jsonify, render_template, request

from smartc.pipeline import analyze

app = Flask(__name__)

SAMPLE = """int compute(int a, int b) {
    int c1 = 2 + 3;
    int sum = a + b;
    if (sum > 100) {
        return sum - 1;
    }
    return sum + c1;
}

int main() {
    int value = compute(10, 20);
    return 0;
}
"""


@app.route("/")
def index():
    return render_template("index.html", sample=SAMPLE)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    source = data.get("source", "")
    if not source.strip():
        return jsonify({"error": "No source code provided."}), 400
    try:
        result = analyze(source)
    except Exception as exc:  # pragma: no cover - defensive, surfaces in UI
        return jsonify({"error": f"Internal error: {exc}"}), 500
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True)
