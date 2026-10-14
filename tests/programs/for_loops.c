#include <stdio.h>

int main() {
    int i;
    int j;
    int total = 0;
    for (i = 0; i < 5; i++) {
        total = total + i;
    }
    for (int k = 10; k > 0; k = k - 3) {
        printf("k=%d ", k);
    }
    for (i = 0; i < 4; i++) {
        for (j = 0; j < 4; j++) {
            if (j == i) {
                continue;
            }
            if (j > 2) {
                break;
            }
            total = total + i * j;
        }
    }
    i = 0;
    for (;;) {
        i++;
        if (i >= 7) {
            break;
        }
    }
    int n = 3;
    while (n > 0) {
        total = total + n;
        n--;
    }
    printf("\n%d %d\n", total, i);
    return 0;
}
