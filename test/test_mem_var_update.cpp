#include <vector>
#include <map>
#include <string>
#include <tuple>
#include <map>
#include <unordered_map>

struct Test
{
    int a = 1;
    float b = 1.5f;
    long c = 2;
    std::string d = "Hello, World!";
    std::map<int, std::string> e
    {
        {1, "one"},
        {2, "two"},
        {3, "three"}
    };

    std::unordered_map<std::string, int> f
    {
        {"apple", 1},
        {"banana", 2},
        {"cherry", 3}
    };
};

struct TestWrapper
{
    Test test {};
    std::vector<int> vec{1, 2, 3, 4, 5};
    std::tuple<int, float, std::string> tup{42, 3.14f, "tuple"};
};

void f()
{
    TestWrapper w {};

    w.test.a = 30;
    w.test.b = 95.6f;
    w.test.c = 7890123L;
    w.test.d = "Updated string in f()";
}

TestWrapper w {};

int main()
{
    TestWrapper w {};
    TestWrapper r {};

    w.test.a = 10;
    w.test.b = 20.5f;

    auto &rw = w;
    f();

    w.test.c = 30;
    w.test.d = "Updated string in main()";

    {
        TestWrapper w {};

        w.test.a = 40;
        w.test.b = 50.5f;

        w.test.c = 60;
        w.test.d = "Updated string in main() inner scope";

        { int w = 23; w = 12; {int w = 55; w = 144; }}
    }

    {
        int w = 12;
        {
            float w = 23.0;
            {
                int w = 14;
                {
                    float w = 72.1;
                    {
                        TestWrapper w {};
                        {
                            std::string w {};
                            {
                                TestWrapper w {};
                                {
                                    std::map<int, std::string> w
                                    {
                                        {1, "one"},
                                        {2, "two"},
                                        {3, "three"}
                                    };
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    return 0;
}
