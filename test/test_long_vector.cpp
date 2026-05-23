#include <vector>
#include <numeric>

int main()
{
    std::vector<int> v;
    v.resize(100000);

    std::iota(v.begin(), v.end(), 0);
    return 0;
}
