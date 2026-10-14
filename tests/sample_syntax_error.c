/* sample_syntax_error.c
 * A missing semicolon after `int a = 5` -- demonstrates syntax-error
 * reporting with a line number.
 */

int main() {
    int a = 5
    int b = 10;
    return a + b;
}
