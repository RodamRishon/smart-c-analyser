#include <stdio.h>

int level = 100;

int main() {
    int a = 1;
    {
        int b = a + 1;
        {
            int a = b * 10;
            int c = a + level;
            printf("%d %d %d\n", a, b, c);
        }
        a = a + b;
    }
    if (a > 0) {
        int level = 7;
        printf("%d %d\n", a, level);
    }
    printf("%d\n", level);
    return 0;
}
