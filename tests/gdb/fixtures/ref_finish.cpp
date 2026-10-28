// ``int &v`` passed a -> b keeps the same BindingKey (name, referent address)
// after ``finish``; the locals pane must keep updating it in a().
#include <cstdio>

__attribute__((noinline)) void b(int &v)
{
    v += 1; // MARK_B
}

__attribute__((noinline)) void a(int &v)
{
    b(v);
    v += 10;
    v += 100;
    v += 1000;
}

int main()
{
    int c = 0;
    a(c);
    std::printf("%d\n", c);
    return 0;
}
