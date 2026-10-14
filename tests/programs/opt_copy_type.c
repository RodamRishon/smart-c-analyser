#include <stdio.h>
int trunc2(double x) { int i = x; return i * 2; }
double widen(int a) { double d = a; return d / 4; }
int main() { printf("%d %f\n", trunc2(2.7), widen(3)); return 0; }
