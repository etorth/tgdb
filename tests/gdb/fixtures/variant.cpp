// std::variant switching alternatives while expanded in the Locals pane:
// the child layout changes shape ([contained value] -> [0],[1],[2]), which
// crashes GDB's varobj cache unless the binding is re-created.
#include <string>
#include <variant>
#include <vector>

int main()
{
    std::variant<std::string, std::vector<int>, int, float> v{};

    v = std::string{"string"};
    v = std::vector<int>{1, 2, 3}; // MARK_TO_VECTOR
    v = int(20);
    v = float(0.25);
    return 0;
}
