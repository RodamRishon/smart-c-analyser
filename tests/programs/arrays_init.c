#include <stdio.h>

int primes[5] = {2, 3, 5, 7, 11};
double weights[] = {0.5, 1.5, -2.25};
int table[2][3] = {{1, 2, 3}, {4, 5, 6}};

int main() {
    int arr[5] = {10, 20, 30, 40, 50};
    int partial[6] = {1, 2};
    int inferred[] = {4, 8, 15, 16, 23, 42};
    int flat2d[2][3] = {1, 2, 3, 4};
    int rows[][2] = {{9, 8}, {7}, {6, 5}};
    char word[] = "hello";
    char padded[8] = "hi";
    int zeros[4] = {0};
    int i;
    int j;
    int sum = 0;

    for (i = 0; i < 5; i++) {
        sum = sum + arr[i] + primes[i];
    }
    for (i = 0; i < 6; i++) {
        printf("%d %d ", partial[i], inferred[i]);
    }
    printf("\n");
    for (i = 0; i < 2; i++) {
        for (j = 0; j < 3; j++) {
            printf("%d/%d ", table[i][j], flat2d[i][j]);
        }
    }
    printf("\n");
    for (i = 0; i < 3; i++) {
        printf("%d,%d %.2f ", rows[i][0], rows[i][1], weights[i]);
    }
    printf("\n%s %s %d %d\n", word, padded, padded[5], zeros[3]);
    arr[2] = arr[1] * 2;
    printf("%d %d\n", sum, arr[2]);
    return 0;
}
