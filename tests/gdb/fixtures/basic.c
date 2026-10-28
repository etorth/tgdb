// Small program for breakpoint / source / locals smoke tests.
#include <stdio.h>

int add(int a, int b)
{
    int result = a + b; // MARK_ADD
    return result;
}

int factorial(int n)
{
    if (n <= 1)
        return 1;
    return n * factorial(n - 1);
}

int main(void)
{
    int x = 10; // MARK_MAIN_FIRST
    int y = 20;
    int sum = add(x, y);
    printf("sum = %d\n", sum);

    int fact = factorial(5);
    printf("fact = %d\n", fact); // MARK_MAIN_FACT
    return 0;
}
