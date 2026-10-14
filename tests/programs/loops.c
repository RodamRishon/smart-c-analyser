int main() {
    int sum = 0;
    int i;
    for (i = 0; i < 10; i++) {
        sum = sum + i * 2;
    }
    int j = 0;
    while (j < 5) {
        sum += j;
        j++;
        if (j == 3) { continue; }
        if (sum > 1000) { break; }
    }
    return sum % 256;
}
