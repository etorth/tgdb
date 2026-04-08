#include <vector>
struct MemPtr
{
    char * ptr = reinterpret_cast<char *>(0x1);
};

int main()
{
    MemPtr p {};
    return 0;
}
