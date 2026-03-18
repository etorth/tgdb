#include <iostream>
#include <vector>
#include <map>
#include <string>
#include <expected>
#include <algorithm>
#include <tuple>
#include <variant>
#include <coroutine>

// 1. Stressing the preprocessor and attributes
#define STRANGE_MACRO(x) #x
[[nodiscard("Reason")]] 
auto test_attributes() { return 42; }

// 2. Type aliases and expected (moved for visibility)
using StringMap = std::map<std::string, std::vector<std::string>, std::less<>>;

std::expected<int, std::string> get_value(bool fail) {
    if (fail) return std::unexpected("error: _ is not a number");
    return 42;
}

namespace Testing::Sub {
    // Testing C++20/23/26 Template Syntax & Constraints
    template <typename T>
    concept CanBeWired = requires(T a) {
        { a.bit_cast() } -> std::same_as<uint32_t>;
    };

    template <typename... Args>
    struct VariadicPack : Args... {
        using Args::operator()...; 
    };

    struct Deducer {
        template <typename Self>
        void identity(this Self&& self) {
            std::cout << "Type: " << typeid(self).name() << "\n";
        }
    };
}

// 3. Literals and Raw Strings
void literals_test() {
    auto hex_bytes = 0xDE'AD'BE'EF;
    auto binary    = 0b1010'0101;
    auto float_val = 1.234'567e-10f;

    const char* raw_str = R"delimiter(
        Inside a raw string: " \ '
        No escape needed here.
    )delimiter";

    const char8_t* u8_str = u8"UTF-8 string";
}

// 4. Coroutines
struct Task {
    struct promise_type {
        Task get_return_object() { return {}; }
        std::suspend_never initial_suspend() { return {}; }
        std::suspend_never final_suspend() noexcept { return {}; }
        void return_void() {}
        void unhandled_exception() {}
        std::suspend_always yield_value(int) { return {}; }
    };
};

Task async_func() {
    co_await std::suspend_always{};
    co_yield 42;
    co_return;
}

int main() {
    // --- BEGIN EXECUTABLE BLOCK (50+ lines) ---
    auto result = test_attributes();
    literals_test();

    /* * * /  <-- sneaky comment end */

    StringMap registry;
    registry["cpp26"] = {"placeholder", "pack_indexing", "structured_bindings"};
    registry["legacy"] = {"macros", "void*", "goto"};

    std::string normal = "Standard String";
    std::u8string utf8 = u8"UTF-8: \u2713";
    
    // Testing string concatenation and weird spacing
    std::string long_literal = "This string" 
                               " is concatenated"
                               " by the preprocessor";

    // C++26 Placeholder _ in structured bindings
    for (const auto& [key, _] : registry) {
        std::cout << "Key: " << key << "\n";
    }

    auto res = get_value(false);
    if (res) {
        int val = *res;
        std::cout << "Value: " << val << "\n";
    }

    // Deducing this with explicit return type for recursion
    auto processor = []<typename T>(this auto self, T value) -> int {
        if (value > 0) return value + self(value - 1);
        return 0;
    };
    int recursive_sum = processor(5);

    // Variant and Visit
    std::variant<int, std::string> var = "Testing Syntax";
    std::visit([](auto&& arg) {
        using T = std::decay_t<decltype(arg)>;
        if constexpr (std::is_same_v<T, int>) std::cout << "int\n";
        else if constexpr (std::is_same_v<T, std::string>) std::cout << "str\n";
    }, var);

    // Indentation stress: Dangling else and Labels
    int x = 10, y = 20;
    if (x > 5)
        if (y > 10)
            std::cout << "Inner if\n";
    else
        std::cout << "Belongs to inner if\n";

    // Label and Goto
    goto skip_label;
    std::cout << "Unreachable\n";
    skip_label:

    // Bit manipulation with digit separators
    uint32_t mask = 0b1111'0000'1010'0101;
    if (mask & (1 << 4)) {
        static_assert(true);
        std::cout << "Bit 4 set\n";
    }

    // Testing comments and formatting inside expressions
    std::vector<int> nums = { 1, 
                              2 /* mid-list comment */, 
                              3 };

    auto count = std::count_if(nums.begin(), nums.end(), [](int n) {
        return n % 2 == 0;
    });

    std::cout << "Final: " << count << " Sum: " << recursive_sum << "\n";

    return 0;
    // --- END EXECUTABLE BLOCK ---
}
