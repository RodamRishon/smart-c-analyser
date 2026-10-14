int f(int x) {
    int a = x + 0;
    int b = x * 1;
    int c = x - 0;
    int d = x * 0;
    return a + b + c + d;
}
int main() {
    return f(7) % 256;
}
