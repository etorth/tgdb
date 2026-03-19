#include <iostream>
#include <vector>
#include <map>
#include <string>
#include <expected>
#include <algorithm>
#include <tuple>
#include <variant>
#include <coroutine>
#include <ranges> // Added for C++20 Ranges

// 1. Stressing the preprocessor and attributes
#define STRANGE_MACRO(x) #x
[[nodiscard("Reason")]]
auto test_attributes() { return 42; }

// 2. Type aliases and expected
using StringMap = std::map<std::string, std::vector<std::string>, std::less<>>;

std::expected<int, std::string> get_value(bool fail) {
    if (fail) return std::unexpected("错误: _ 不是数字"); // Chinese in std::expected
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

// 3. ENHANCED: Literals, Raw Strings, and CJK
void literals_test() {
    auto hex_bytes = 0xDE'AD'BE'EF;
    auto binary    = 0b1010'0101;
    auto float_val = 1.234'567e-10f;
    auto 😊 = "emoji";
    auto 😊😊 = "emoji x 2";
    auto 😊😊😊 = "emoji x 3";

    const char* raw_str = R"delimiter(
        Inside a raw string: " \ '
        No escape needed here.
    )delimiter";

    const char8_t* u8_str = u8"UTF-8 string";

    // CJK and Unicode prefixes
    const char8_t* u8_cjk = u8"UTF-8: 汉字, 日本語, 한국어, 😊"; // Testing multi-byte widths
    const char16_t* u16_val = u"Unicode: \u4F60\u597D";       // "Hello" in Chinese
    const char32_t* u32_val = U"Emoji: \U0001F343";           // Leaf emoji

    // Raw String with CJK delimiters and content
    const char* raw_cjk = R"zhongwen(
        Multiline CJK test:
        - 程序员 (Programmer)
        - 调试 (Debug)
        - 😊
    )zhongwen";

    const char8_t* raw_u8 = u8R"---(Mixed "Raw" and 漢字 and 😊)---";
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
    // THIS IS A VERY LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LONG LINE
    // --- BEGIN EXECUTABLE BLOCK (80+ lines) ---
    auto result = test_attributes();
    literals_test();

    /* * 这是一个多行注释测试 (Multi-line CJK comment)
     * * * /  <-- sneaky comment end
     */

    /* * * /  <-- sneaky comment end */

    StringMap registry;
    registry["cpp26"] = {"placeholder", "pack_indexing", "structured_bindings"};
    registry["legacy"] = {"macros", "void*", "goto", "😊"};


    registry["cjk_test"] = {"开发", "テスト", "코드", "😊"}; // CN, JP, KR, Emoji
                                                       //

    std::string normal = "Standard String";
    std::u8string utf8 = u8"UTF-8: \u2713, 😊";

    std::string emoji_test = "Emoji test: \U0001F680";
    std::string escape_hell = "Tab\tNewline\nHex\x0AOctal\012";

    std::string long_literal = "This string"
                               " is concatenated"
                               " with \x41\x42\x43";

    // C++26 Placeholder _ in structured bindings
    for (const auto& [key, _] : registry) {
        std::cout << "Key: " << key << "\n";
    }

    auto res = get_value(false);
    if (res) {
        int val = *res;
        std::cout << "Value: " << val << " 😊"<< "\n";
    }

    // C++20/23 RANGES STRESS TEST
    std::vector<int> source_data = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
    auto view = source_data
        | std::views::filter([](int n) { return n % 2 == 0; })
        | std::views::transform([](int n) { return n * n; })
        | std::views::take(3);

    std::cout << "Ranges: ";
    for (int n : view) { std::cout << n << " "; }
    std::cout << "\n";

    // Testing member pointers and projections
    struct Item { int id; std::string name; };
    std::vector<Item> items = {{3, "C++"}, {1, "Python"}, {2, "Vim"}};
    std::ranges::sort(items, {}, &Item::id);

    // Deducing this recursion
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

    // Indentation stress: Dangling else
    int x = 10, y = 20;
    if (x > 5)
        if (y > 10)
            std::cout << "Deep if\n";
    else
        std::cout << "Dangling else\n";

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
                              2 /* 注释 */,
                              3 };

    auto count = std::count_if(nums.begin(), nums.end(), [](int n) {
        return n % 2 == 0;
    });

    std::cout << "Final: " << count << " Sum: " << recursive_sum << "\n";

    return 0;
    // --- END EXECUTABLE BLOCK ---
}
