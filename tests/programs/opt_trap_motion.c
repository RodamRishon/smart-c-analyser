#include <stdio.h>
int div(int a, int b) { int q = a / b; printf("before\n"); return q; }
int main() { printf("%d\n", div(10, 2)); return 0; }
