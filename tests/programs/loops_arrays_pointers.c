#include <stdio.h>

int sum_array(int *a, int n) {
    int s = 0;
    for (int i = 0; i < n; i++) {
        s += a[i];
    }
    return s;
}

void fill_squares(int *a, int n) {
    for (int i = 0; i < n; i++) {
        a[i] = i * i;
    }
}

int find(int *a, int n, int key) {
    int idx = -1;
    int i = 0;
    while (i < n) {
        if (a[i] == key) {
            idx = i;
            break;
        }
        i++;
    }
    return idx;
}

void reverse(int *a, int n) {
    int lo = 0;
    int hi = n - 1;
    while (lo < hi) {
        int t = a[lo];
        a[lo] = a[hi];
        a[hi] = t;
        lo++;
        hi--;
    }
}

int walk(int *p, int n) {
    int s = 0;
    int *end = p + n;
    while (p != end) {
        s += *p;
        p++;
    }
    return s;
}

int safe_sum(int *p, int n) {
    int s = 0;
    int i = 0;
    while (p != NULL && i < n && p[i] >= 0) {
        s += p[i];
        i++;
    }
    return s;
}

int m[3][3] = {{1, 2, 3}, {4, 5, 6}, {7, 8, 9}};

int matrix_trace() {
    int t = 0;
    for (int i = 0; i < 3; i++) {
        t += m[i][i];
    }
    return t;
}

int main() {
    int a[8];
    fill_squares(a, 8);
    printf("%d\n", sum_array(a, 8));
    printf("%d %d\n", find(a, 8, 25), find(a, 8, 26));
    reverse(a, 8);
    printf("%d %d %d\n", a[0], a[3], a[7]);
    printf("%d\n", walk(a, 8));
    int b[5] = {3, 4, 5, -1, 9};
    printf("%d\n", safe_sum(b, 5));
    int *np = NULL;
    printf("%d\n", safe_sum(np, 5));
    printf("%d\n", matrix_trace());
    return 0;
}
