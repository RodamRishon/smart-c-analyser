int sq(int n) { return n * n; }
int calc(int a, int b) {
    int p = a * b + 3;
    int q = a * b + 3;
    int r = (a + b) * (b + a);
    return p + q + r + sq(a) + sq(a);
}
int main() {
    int dead = 99;
    int c = 2 + 3 * 4;
    if (1) { c = c + 1; } else { c = 0; }
    if (0) { c = 777; }
    return (calc(3, 4) + c) % 256;
}
