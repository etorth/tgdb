// Locals whose type lives in an anonymous namespace cannot be created with
// ``*(type*)addr``; a shadowed outer one is a placeholder until the inner
// scope ends, then it is promoted to a real varobj.
#include <string>

int g_strcnt = 0;

namespace
{
    struct A
    {
        int a;
        std::string s;
        A() : a(g_strcnt++), s(std::to_string(a)) {}
    };
}

int main()
{
    A a{};
    {
        A a{};
        {
            A a{};
            a.a += 100;
        }
        int c = 10; // MARK_MIDDLE
        c += a.a;
    }
    return a.a; // MARK_OUTER
}
