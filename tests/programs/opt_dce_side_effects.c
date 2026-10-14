#include <stdio.h>
int f(int a, int b) { int q = a / b; int *p = &a; int v = *p; printf("hi\n"); return a; }
int main() { printf("%d\n", f(10, 2)); return 0; }
