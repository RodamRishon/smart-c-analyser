#include <stdio.h>

int square(int n);

int factorial(int n) {
    if (n <= 1) {
        return 1;
    }
    return n * factorial(n - 1);
}

int add3(int a, int b, int c) {
    return a + b + c;
}

void report(int value) {
    printf("value=%d\n", value);
}

int square(int n) {
    return n * n;
}

double average(int a, int b) {
    return (a + b) / 2.0;
}

int main() {
    int f = factorial(5);
    int s = add3(square(2), square(3), factorial(3));
    report(f);
    report(s);
    printf("%.1f\n", average(3, 4));
    return 0;
}
