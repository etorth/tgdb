// Expanded containers and structs across steps and frame switches.
#include <vector>

struct Point
{
    int x;
    int y;
};

__attribute__((noinline)) int use(Point pt)
{
    int z = pt.x + pt.y; // MARK_USE
    return z;
}

int main()
{
    std::vector<int> v;
    v.push_back(1); // MARK_PUSH
    v.push_back(2);
    v.push_back(3);
    Point p{3, 4};
    return use(p) + (int)v.size();
}
