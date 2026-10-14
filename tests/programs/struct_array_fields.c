struct Box {
    int vals[3];
};

struct Boxes {
    struct Box items[2];
};

int main() {
    struct Box b;
    int i = 1;
    b.vals[i] = 5;
    b.vals[i + 1] = 7;

    struct Box *pb = &b;
    pb->vals[0] = 42;

    struct Boxes bs;
    bs.items[0].vals[0] = 100;
    bs.items[1].vals[1] = 200;

    int total = b.vals[i] + b.vals[i + 1] + pb->vals[0]
              + bs.items[0].vals[0] + bs.items[1].vals[1];
    return total % 256;
}
