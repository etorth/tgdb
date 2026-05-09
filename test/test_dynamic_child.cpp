#include <vector>

struct Simple
{
    int a = 12;
    std::vector<int> b {};
};

int main()
{
    Simple s {};

    s.b.push_back(1);
    s.b.push_back(2);
    s.b.push_back(3);

    return 0;
}
