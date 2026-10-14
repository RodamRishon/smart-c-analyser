#include <stdio.h>
#include <stdlib.h>
int main() { int *p = NULL; int ok = 0; if (p != NULL && *p > 0) { ok = 1; } printf("%d\n", ok); return 0; }
