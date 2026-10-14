union Value {
    int i;
    float f;
};

int main() {
    union Value v;
    v.i = 65;
    int x = v.i;
    return x % 256;
}
