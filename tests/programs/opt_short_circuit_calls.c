#include <stdio.h>
int hits = 0;
int f() { hits = hits + 1; printf("f\n"); return 0; }
int g() { hits = hits + 10; printf("g\n"); return 1; }
int main() { int r = f() && g(); int s = g() || f(); printf("%d %d %d\n", r, s, hits); return 0; }
