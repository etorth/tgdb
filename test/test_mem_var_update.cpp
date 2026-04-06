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

    fw.test.a = 20;
    fw.test.b = 45.6f;
    fw.test.d = "Updated string";
}

int main()
{
    TestWrapper w {};

    w.test.a = 20;
    w.test.b = 45.6f;
    w.test.d = "Updated string";

    f();
    return 0;
}
