/* sample_valid.c
 * A clean program with deliberate optimisation opportunities:
 *  - constant folding        (c1 = 2 + 3, then c2 = c1 * 4)
 *  - common subexpression    (sum1 and sum2 both compute a + b, and
 *                              since a, b are parameters -- not known
 *                              constants -- folding can't remove the
 *                              repeated computation; CSE does)
 *  - dead store elimination  (the first value written to `dead` is
 *                              overwritten before it is ever read)
 *  - an unused variable      (unused_var), flagged by semantic analysis
 *  - unreachable code        (statements placed after `return`, which
 *                              can never execute)
 */

int compute(int a, int b) {
    int unused_var;
    int result;

    int c1 = 2 + 3;
    int c2 = c1 * 4;

    int sum1 = a + b;
    int sum2 = a + b;

    int dead = a * b;
    dead = sum1 + sum2;

    result = dead + c2;

    if (result > 100) {
        result = result - 1;
    } else {
        result = result + 1;
    }

    int i;
    int total = 0;
    for (i = 0; i < 5; i = i + 1) {
        total = total + i;
    }

    return result + total;

    int junk = 999;
    junk = junk + 1;
}

int main() {
    int value;
    value = compute(10, 20);
    return 0;
}
