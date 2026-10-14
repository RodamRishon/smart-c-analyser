#include <stdlib.h>

void fill(int *arr, int n) {
    int i;
    for (i = 0; i < n; i++) {
        arr[i] = i * i;
    }
}

int sum_array(int *arr, int n) {
    int total = 0;
    int i;
    for (i = 0; i < n; i++) {
        total = total + arr[i];
    }
    return total;
}

int main() {
    int n = 6;
    int *arr = (int*) malloc(n * sizeof(int));
    if (arr == NULL) {
        return 1;
    }
    fill(arr, n);
    int s = sum_array(arr, n);
    free(arr);

    char *buf = (char*) malloc(3);
    buf[0] = 'h';
    buf[1] = 'i';
    buf[2] = '\0';
    int first = buf[0];
    free(buf);

    return (s + first) % 256;
}
