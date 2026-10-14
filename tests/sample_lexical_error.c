/* sample_lexical_error.c
 * Contains an invalid character ('@') and a malformed numeric literal
 * (12abc) -- demonstrates lexical-error reporting with line numbers.
 */

int main() {
    int a = 5;
    int b = a @ 2;
    int c = 12abc;
    return b;
}
