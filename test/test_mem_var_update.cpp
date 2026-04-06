#include <vector>
#include <map>
#include <string>
#include <tuple>
#include <map>
#include <unordered_map>

struct Test
{
    int a = 12;
    float b = 23.2;
    long c = 1234567890123456789L;
    std::string d = "Hello, C++26!";
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
    TestWrapper fw {};

    fw.test.a = 30;
    fw.test.b = 95.6f;
    fw.test.c = 7890123L;
    fw.test.d = "Updated string in f()";
}

int main()
{
    TestWrapper w {};

    w.test.a = 10;
    w.test.b = 45.6f;

    f();

    w.test.c = 4567890L;
    w.test.d = "Updated string in main()";

    return 0;
}
