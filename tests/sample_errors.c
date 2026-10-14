/* sample_errors.c
 * Parses cleanly (no lexical/syntax errors) but is full of semantic
 * mistakes, to exercise the semantic analyser:
 *  - redeclaration of a variable in the same scope (x)
 *  - use of an undeclared identifier (y)
 *  - a type mismatch on assignment (string literal into an int)
 *  - a function call with the wrong number of arguments
 *  - a scope error: using a block-local variable after its block ends
 */

int add(int a, int b) {
    return a + b;
}

int main() {
    int x = 5;
    int x = 10;

    y = 20;

    int flag = "hello";

    int total = add(x, 10, 20);

    if (flag) {
        int local = 1;
    }
    local = 2;

    return total;
}
