int main()
{
    int a = 0;
    int *p = &a;
    {
        int a = 12;
        *p = 8;
        int c = 10;
    }
    return 0;
}
