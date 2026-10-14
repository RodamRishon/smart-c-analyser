int g;
int arr[4];
void bump() { g = g + 5; }
int main() {
    int x = 1;
    {
        int x = 2;
        g = x * 10;
    }
    bump();
    int a[3];
    int k;
    for (k = 0; k < 3; k++) { a[k] = k * k + x; }
    arr[2] = a[1] + a[2];
    return (g + arr[2] + x) % 256;
}
