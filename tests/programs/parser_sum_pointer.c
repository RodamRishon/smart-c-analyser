#include <stdio.h>

int main() {
    int arr[5] = {10, 20, 30, 40, 50};
    int sum = 0;
    int i;

    for (i = 0; i < 5; i++) {
        sum = sum + arr[i];
    }

    int *ptr = &sum;
    *ptr = *ptr + 0;

    printf("Sum: %d\n", sum);
    return 0;
}
