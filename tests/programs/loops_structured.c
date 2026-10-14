#include <stdio.h>

int sum_to(int n) {
    int total = 0;
    for (int i = 1; i <= n; i++) {
        total += i;
    }
    return total * 2;
}

int count_digits(int n) {
    int c = 0;
    while (n > 0) {
        n = n / 10;
        c++;
    }
    return c;
}

int first_multiple(int limit, int k) {
    int i;
    for (i = 1; i < limit; i++) {
        if (i % k == 0) {
            break;
        }
    }
    return i;                       /* i is used after the loop */
}

int skip_odds(int n) {
    int s = 0;
    for (int i = 0; i < n; i++) {
        if (i % 2 == 1) {
            continue;
        }
        s += i;
    }
    return s;
}

int nested(int n) {
    int hits = 0;
    for (int i = 0; i < n; i++) {
        for (int j = 0; j < n; j++) {
            if (j > i) {
                break;              /* leaves the inner loop only */
            }
            if ((i + j) % 3 == 0) {
                continue;
            }
            hits += i * j;
        }
    }
    return hits;
}

int forever(int target) {
    int x = 0;
    while (1) {
        x += 3;
        if (x > target) {
            break;
        }
    }
    return x;
}

int early_return(int n) {
    for (int i = 0; i < 100; i++) {
        if (i * i > n) {
            return i;
        }
    }
    return -1;
}

int main() {
    printf("%d\n", sum_to(10));
    printf("%d\n", count_digits(98765));
    printf("%d %d\n", first_multiple(50, 7), first_multiple(5, 7));
    printf("%d\n", skip_odds(11));
    printf("%d\n", nested(6));
    printf("%d\n", forever(20));
    printf("%d %d\n", early_return(50), early_return(100000));
    return 0;
}
