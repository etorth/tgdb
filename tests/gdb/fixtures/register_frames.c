// Built with -O2: ``k`` lives in a register in both frames, so both have the
// same BindingKey ("k", "register@0").  The locals pane must show leaf's k in
// leaf and main's k after ``up``.
#include <stdio.h>

__attribute__((noinline)) int leaf(int seed)
{
    int k = seed * 3 + 1;
    __asm__ volatile("" : "+r"(k));
    __asm__ volatile("nop"); // MARK_LEAF
    __asm__ volatile("" : "+r"(k));
    return k;
}

int main(int argc, char **argv)
{
    (void)argv;
    int k = argc + 100;
    __asm__ volatile("" : "+r"(k));
    int r = leaf(k);
    __asm__ volatile("" : "+r"(k));
    printf("%d %d\n", r, k);
    return 0;
}
