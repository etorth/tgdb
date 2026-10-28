// An inner ``a`` shadows the outer one; the outer is written through a
// pointer while shadowed, so its (re-pinned) row must keep updating.
int main()
{
    int a = 0;
    int *p = &a;
    {
        int a = 12;
        *p = 8; // MARK_WRITE_OUTER
        int c = 10;
        c += a;
    }
    a += 1; // MARK_AFTER_BLOCK
    return a;
}
