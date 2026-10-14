#include <stdio.h>

void swap(int *a, int *b) {
    int t = *a;
    *a = *b;
    *b = t;
}

int main() {
    int sum = 40;
    int *ptr = &sum;
    *ptr = *ptr + 0;
    *ptr = *ptr + 2;
    int x = 3;
    int y = 9;
    swap(&x, &y);
    int arr[4] = {1, 2, 3, 4};
    int *p = &arr[1];
    *(p + 1) = 30;
    p = p + 2;
    int **pp = &ptr;
    **pp = **pp + 1;
    int *none = NULL;
    int ok = 0;
    if (none == NULL) {
        ok = 1;
    }
    printf("%d %d %d %d %d %d\n", sum, x, y, arr[2], *p, ok);
    return 0;
}
