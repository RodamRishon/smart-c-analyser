#include <stdio.h>
int main() {
    int arr[5] = {10, 20, 30, 40, 50};
    int i;
    int sum = 0;
    for (i = 0; i < 5; i++) {
        sum = sum + arr[i];
    }
    int *ptr = arr;
    int total = 0;
    for (i = 0; i < 5; i++) {
        total = total + ptr[i];
    }
    printf("Sum: %d Total: %d\n", sum, total);
    return 0;
}
