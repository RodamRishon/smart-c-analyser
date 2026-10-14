double scale(double v, int k) { return v * k + 0.5; }
int main() {
    double d = 1.5 * 2;
    double e = scale(d, 4);
    int r = e;
    return r;
}
