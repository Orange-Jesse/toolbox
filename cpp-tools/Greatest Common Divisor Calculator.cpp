#include <stdio.h>
int zuida(int x, int y) 
{
    int r;
    while (x % y != 0) 
    {
        r = x % y;
        x = y;
        y = r;
    }
    return y;
}
int main() 
{
    int x, y, f;
    printf("please enter two numbers:\n");
    scanf_s("%d %d", &x, &y);
    f = zuida(x, y);
    printf("%d", f);
    return 0;
}