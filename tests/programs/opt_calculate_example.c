#include <stdio.h>

int calculate(int x, int y) {
    int a = 10 + 20;
    int b = 5 * 4;
    int c = a + b;

    int unused = 999;

    int result = x + y;
    int duplicate = x + y;

    if (1) {
        result = result + 0;
    }

    if (0) {
        result = result + 1000;
    }

    int final_result = c + result;

    return final_result;
}

int main() {
    int answer = calculate(10, 20);
    printf("Result: %d\n", answer);
    return 0;
}
