#include <stdio.h>
int f(int a) { int x = a + 1; { int x = a * 2; x = x + a; printf("%d\n", x); } x = x * 3; printf("%d\n", x); return x; }
int main() { printf("%d\n", f(7)); return 0; }
