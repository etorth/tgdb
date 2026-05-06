int g_intcnt = 0;

void f(int r)
{
    if(r <= 0){
        return;
    }

    int a = g_intcnt++;
    f(r - 1);
}

int main()
{
    f(10);
    return 0;
}
