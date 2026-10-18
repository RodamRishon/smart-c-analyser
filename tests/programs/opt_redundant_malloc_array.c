#include <stdio.h>
#include <stdlib.h>

int main() {
    int n = 5;

    int *arr = (int *)malloc(n * sizeof(int));

    if (arr == NULL) {
        printf("Memory allocation failed\n");
        return 1;
    }

    arr[0] = 10;
    arr[1] = 20;
    arr[2] = 30;
    arr[3] = 40;
    arr[4] = 50;

    int sum = 0;

    for (int i = 0; i < n; i++) {
        sum = sum + arr[i];
    }

    int result = sum * 2;
    int duplicate = sum * 2;

    int first = *arr;
    int second = *arr;

    printf("Array Sum: %d\n", sum);
    printf("Result: %d\n", result);
    printf("Duplicate: %d\n", duplicate);
    printf("First Element: %d\n", first);
    printf("Second Element: %d\n", second);

    free(arr);

    return 0;
}
