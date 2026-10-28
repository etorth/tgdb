// Value formatting: escaped backslashes, UTF-8 text.
#include <stdio.h>

int main(void)
{
    char path[] = "C:\\2024\\x";
    const char *smile = "caf\xc3\xa9 \xf0\x9f\x98\x80";
    int n = 42;
    printf("%s %s %d\n", path, smile, n); // MARK_PRINT
    return 0;
}
