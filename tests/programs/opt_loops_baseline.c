#include <stdio.h>
int sum_to(int n) {
    int total = 0;
    int i;
    for (i = 0; i < n; i++) {
        if (i == 3) continue;
        total = total + i;
    }
    int j = 0;
    while (j < n) {
        if (total > 100) break;
        total += j;
        j++;
    }
    int result = total * 2;
    return result;
}
void touch(int *p, int *a) { *p = *p; a[1] = a[1]; }
int main() { int x = 4; int arr[3] = {1,2,3}; touch(&x, arr); printf("%d %d\n", sum_to(10), x + arr[1]); return 0; }
