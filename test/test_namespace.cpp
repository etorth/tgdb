#include <string>

int g_strcnt = 0;
int g_intcnt = 0;

namespace
{
    struct A
    {
        int a;
        std::string s;
        A(): a(g_strcnt++), s(std::to_string(a)) {}
    };
}


int main()
{
    A a{};
    int b = g_intcnt++;

    {
        A a{};
        int b = g_intcnt++;

        {
            A a{};
            int b = g_intcnt++;
        }

        int c = 10;
        int d = 10;
    }
    return 0;
}
