// Proposal Example 2: constant folding, CSE, dead code.
// (operands are parameters so CSE has something to find;
//  constants alone are folded away entirely by propagation)
int main() {
    int x, y, z, w;
    x = 2 + 3;
    y = x * 4;
    z = x * 4;
    w = 100;
    if (0) {
        w = 999;
    }
    return 0;
}
