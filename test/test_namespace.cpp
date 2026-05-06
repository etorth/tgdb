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
