#include <stdlib.h>

struct Point {
    int x;
    int y;
};

struct Line {
    struct Point start;
    struct Point end;
};

struct Node {
    int val;
    struct Node *next;
};

int sum_list(struct Node *head) {
    int total = 0;
    struct Node *cur = head;
    while (cur != NULL) {
        total = total + cur->val;
        cur = cur->next;
    }
    return total;
}

int main() {
    struct Point p;
    p.x = 10;
    p.y = 20;
    struct Point *pp = &p;
    pp->x = 99;

    struct Line l;
    l.start.x = 1;
    l.start.y = 2;
    l.end.x = 3;
    l.end.y = 4;

    struct Node *a = (struct Node*) malloc(sizeof(struct Node));
    struct Node *b = (struct Node*) malloc(sizeof(struct Node));
    a->val = 5;
    a->next = b;
    b->val = 6;
    b->next = NULL;
    int listsum = sum_list(a);
    free(a);
    free(b);

    int total = pp->x + p.y + l.start.x + l.start.y + l.end.x + l.end.y + listsum;
    return total % 256;
}
