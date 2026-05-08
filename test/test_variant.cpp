#include <string>
#include <vector>
#include <variant>

int main()
{
    std::variant<std::string, std::vector<int>, int, float> var {};

    var = std::string{"string"};
    var = std::vector<int>{1, 2, 3};
    var = int(20);
    var = float(0.25);

    return 0;
}
