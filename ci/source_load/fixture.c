// Minimal test fixture for CI regression tests.
#include <stdio.h>

int add(int a, int b) {
    int result = a + b;
    return result;
}

int factorial(int n) {
    if (n <= 1) return 1;
    return n * factorial(n - 1);
}

int main() {
    int x = 10;
    int y = 20;
    int sum = add(x, y);
    printf("sum = %d\n", sum);

    int fact = factorial(5);
    printf("fact = %d\n", fact);

    return 0;
}
