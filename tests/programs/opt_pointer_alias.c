#include <stdio.h>
/* b is changed directly and read back through a pointer: the store to b
   is not dead even though no later line names b. */
int main() {
    int a = 14, b = 20;
    int k = 7;
    int *p = &b;
    b = (a * 3 + k) % 1000;
    a = (*p - k) - (a - k);
    printf("%d %d\n", a, b);
    *p = 5;
    printf("%d\n", b + 1);
    return 0;
}
