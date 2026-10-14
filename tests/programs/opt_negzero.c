#include <stdio.h>
double addz(double z) { return z + 0; }
int main() { double m = -1.0; printf("%f\n", addz(m * 0.0)); return 0; }
