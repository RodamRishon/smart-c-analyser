#include <stdio.h>

int calls = 0;

int next(int x) {
    calls++;
    return x + 1;
}

int fact(int n) {
    if (n <= 1) {
        return 1;
    }
    return n * fact(n - 1);
}

int gcd(int a, int b) {
    while (b != 0) {
        int t = a % b;
        a = b;
        b = t;
    }
    return a;
}

int collatz(int n) {
    int steps = 0;
    while (n != 1) {
        if (n % 2 == 0) {
            n = n / 2;
        } else {
            n = 3 * n + 1;
        }
        steps++;
    }
    return steps;
}

int call_in_condition(int limit) {
    int i = 0;
    while (next(i) < limit) {       /* the call runs on every test */
        i = i + 2;
    }
    return i;
}

int call_in_step(int n) {
    int s = 0;
    for (int i = 0; i < n; i = next(i)) {
        s += i;
    }
    return s;
}

int classify(int x) {
    int r;
    if (x < 0) {
        r = -1;
    } else if (x == 0) {
        r = 0;
    } else if (x < 10) {
        r = 1;
    } else {
        r = 2;
    }
    return r;
}

int main() {
    printf("%d\n", fact(6));
    printf("%d\n", gcd(84, 36));
    printf("%d\n", collatz(27));
    printf("%d %d\n", call_in_condition(9), calls);
    printf("%d %d\n", call_in_step(6), calls);
    printf("%d %d %d %d\n", classify(-4), classify(0), classify(7), classify(70));
    return 0;
}
