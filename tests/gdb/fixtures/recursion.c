// Same function at several stack levels: same names, different addresses.
int g_count = 0;

void f(int r)
{
    if (r <= 0)
        return;
    int a = g_count++;
    f(r - 1); // MARK_RECURSE
}

int main(void)
{
    f(10);
    return 0;
}
