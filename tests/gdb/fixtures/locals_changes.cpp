// Locals whose type, size or dynamic type changes between stops.
#include <vector>

struct Base
{
    virtual ~Base() {}
    int b = 1;
};
struct D1 : Base
{
    int d1 = 2;
};
struct D2 : Base
{
    int d2 = 3;
};
struct Holder
{
    Base *p;
    int n;
};

// Two same-named locals of different types in the same stack slot.
__attribute__((noinline)) int as_int()
{
    volatile int x = 7;
    return x; // MARK_INT
}

__attribute__((noinline)) float as_float()
{
    volatile float x = 2.5f;
    return x; // MARK_FLOAT
}

int main()
{
    int total = as_int();
    total += (int)as_float();
    std::vector<int> v{1, 2, 3, 4, 5};
    v.pop_back(); // MARK_POP
    v.pop_back();
    D1 one;
    D2 two;
    Holder h{&one, 5};
    h.p = &two; // MARK_RETARGET
    h.n = 6;
    return total + h.n + (int)v.size();
}
