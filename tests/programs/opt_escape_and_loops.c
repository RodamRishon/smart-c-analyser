#include <stdio.h>
int counter = 3;
void bump_via_pointer(int *p) { *p = *p + 10; }
void bump_global() { counter = counter * 2; }
int main() {
    int x = 5;
    bump_via_pointer(&x);              /* x is no longer the constant 5 */
    printf("%d\n", x + 1);
    int c = counter;                   /* a global the callee changes */
    bump_global();
    printf("%d %d\n", c, counter);
    int sum = 0, i;
    for (i = 0; i < 10; i++) {
        if (i == 2) continue;
        if (i == 7) break;
        sum += i;
    }
    printf("%d %d\n", sum, i);
    float f = 1.5;
    double d = f * 2 + 1;              /* float arithmetic must stay float */
    float g = f / 3;
    printf("%.6f %.6f\n", d, g);
    int arr[3] = {1, 2, 3};
    int *q = arr;
    q[1] = 40;                         /* write through a pointer into the array */
    printf("%d %d\n", arr[1], arr[1] + 1);
    return sum % 256;
}
