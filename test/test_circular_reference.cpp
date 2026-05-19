struct A
{
    int a = 1;
    float b = 0.25f;

    const A *p = this;
};

int main()
{
    A a;
    return 0;
}
