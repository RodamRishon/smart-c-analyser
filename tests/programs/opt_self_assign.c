#include <stdio.h>
struct P { int x; int y; };
int g[4] = {1, 2, 3, 4};
int main() {
    int v = 7;
    int *ptr = &v;
    int arr[5] = {10, 20, 30, 40, 50};
    struct P s;
    s.x = 3; s.y = 4;
    int k = 2;
    *ptr = *ptr;            /* proven: ptr = &v only */
    arr[2] = arr[2];        /* constant in-range index */
    s.x = s.x;              /* member of a real struct */
    arr[k] = arr[k];        /* index not constant: not proven */
    int t = *ptr;           /* load ... */
    *ptr = t;               /* ... stored straight back */
    int u = arr[1];
    arr[1] = 99;            /* changed in between: must stay */
    arr[1] = u;
    printf("%d %d %d %d %d\n", v, arr[2], s.x, arr[1], g[0]);
    return 0;
}
