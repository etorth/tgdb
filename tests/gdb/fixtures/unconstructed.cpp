// dirty() fills the stack with 0x41 so ``s`` holds a wild pointer until its
// constructor runs.  The GCC statement expression gives a stop line after
// s's declaration line but before s is constructed.
#include <cstring>
#include <string>

__attribute__((noinline)) void dirty()
{
    volatile char buf[8192];
    std::memset((char *)buf, 0x41, sizeof buf);
}

__attribute__((noinline)) int work()
{
    int marker = 1;
    std::string s = ({
        int t = marker; // MARK_INSIDE_INIT
        t += 1;
        std::string(t, 'x');
    });
    marker = 2; // MARK_CONSTRUCTED
    marker += (int)s.size();
    return marker;
}

int main()
{
    dirty();
    return work() == 0;
}
