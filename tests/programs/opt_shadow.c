#include <stdio.h>
int main() { int x = 1; { int x = 2; x = x + 5; printf("%d\n", x); } printf("%d\n", x); return 0; }
