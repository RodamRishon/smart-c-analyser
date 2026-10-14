void swap(int *a, int *b) {
    int tmp = *a;
    *a = *b;
    *b = tmp;
}

void increment(int *p) {
    *p = *p + 1;
}

int sum_via_pointer(int *arr, int n) {
    int total = 0;
    int i;
    for (i = 0; i < n; i++) {
        total = total + *(arr + i);
    }
    return total;
}

int main() {
    int x = 3;
    int y = 7;
    swap(&x, &y);
    int before = x + y;
    increment(&x);
    int after = x + y;

    int nums[4];
    nums[0] = 1; nums[1] = 2; nums[2] = 3; nums[3] = 4;
    int s = sum_via_pointer(nums, 4);

    int z = 9;
    int *pz = &z;
    int **ppz = &pz;
    **ppz = 11;

    return (before * 10 + after + s + z) % 256;
}
