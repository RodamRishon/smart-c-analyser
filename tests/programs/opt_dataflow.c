#include <stdio.h>
int main() {
    int n = 4, base = 10, total = 0, scale = 3, i;
    if (n > 2) { base = base + 5; }       /* base is 15 on every path */
    for (i = 0; i < n; i++) {
        total += base * scale;            /* scale is constant, total is not */
    }
    if (total > 100) { scale = 1; } else { scale = 2; }
    printf("%d %d %d %d\n", n, base, total, scale);
    int j = 0;
    while (j < 3) { j = j + 1; base = base + j; }
    printf("%d %d\n", j, base);
    return (total + base) % 256;
}
