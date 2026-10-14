#include <stdio.h>
int f(int c, int d) { c += d; return c + d; }
int main() { printf("%d\n", f(1, 10)); return 0; }
