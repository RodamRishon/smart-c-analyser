#include <stdio.h>
int f1(int p, int q) { return p * 2 + q; }
int main() {
    int c = 17, b;
    b = f1(c % 50, c % 50);   /* the same temporary is read twice by one call */
    printf("%d\n", b);
    return b % 100;
}
