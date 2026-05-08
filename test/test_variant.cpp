#include <string>
#include <vector>
#include <variant>

int main()
{
    std::variant<std::string, std::vector<int>, int, float> v {};

    v = std::string{"string"};
    v = std::vector<int>{1, 2, 3};
    v = int(20);
    v = float(0.25);

    return 0;
}
