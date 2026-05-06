#include <string>

int g_count = 0;
namespace
{
    struct A
    {
        int a;
        std::string s;
        A(): a(g_count++), s(std::to_string(a)) {}
    };
}

int main()
{
    A a{};
    int b = 10;

    {
        A a{};
        int b = 10;

        {
            A a{};
            int b = 10;
        }

        int c = 10;
        int d = 10;
    }
    return 0;
}
