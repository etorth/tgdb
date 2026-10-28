// Locals and Evaluations panes open together while stepping a loop.
#include <stdio.h>

int main(void)
{
    int a = 1;
    int b = 2;
    int arr[3] = {7, 8, 9};
    int i; // MARK_START
    for (i = 0; i < 6; i++) {
        a += 1;
        b += 2;
        arr[i % 3] += 5;
    }
    printf("%d %d %d\n", a, b, arr[0]);
    return 0;
}
