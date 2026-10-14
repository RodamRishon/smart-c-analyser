int cube[2][2][2];

int fill_and_sum(void) {
    int grid[2][3];
    int i, j;
    int sum = 0;
    for (i = 0; i < 2; i++) {
        for (j = 0; j < 3; j++) {
            grid[i][j] = i * 10 + j;
        }
    }
    for (i = 0; i < 2; i++) {
        for (j = 0; j < 3; j++) {
            sum = sum + grid[i][j];
        }
    }
    return sum;
}

int main() {
    cube[0][0][0] = 1;
    cube[1][1][1] = 9;
    int total = fill_and_sum() + cube[0][0][0] + cube[1][1][1];
    return total % 256;
}
