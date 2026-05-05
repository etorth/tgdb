#include <string>

namespace
{
    struct A
    {
        int a = 0;
        std::string s = "empty";
    };
}

int main()
{
    A a{};
    {
        A a{};
        {
            A a{};
        }
    }
    return 0;
}
