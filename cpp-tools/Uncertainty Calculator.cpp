#define _CRT_SECURE_NO_WARNINGS  
#include <stdio.h>
#include <math.h>

#define T_TABLE_ROW 8

int main()
{
    int n = 0;
    float data[100];
    float ave, det, result;
    int degree_of_freedom;
    float machine;

    float t_table[T_TABLE_ROW] = { 4.30 , 3.18 , 2.78 , 2.57 , 2.45 , 2.36 , 2.31 , 2.26 };

    printf("请输入数据，输入0结束：\n");

    while (1)
    {
        float input;
        scanf("%f", &input); 

        if (input == 0)
        {
            break;
        }

        if (n >= 100)
        {
            printf("数据点过多，最多输入100个。\n");
            break;
        }

        data[n++] = input;
    }

    if (n < 4)
    {
        printf("数据点不足，至少需要4个数据。\n");
        return 1;
    }

    ave = 0;
    for (int i = 0; i < n; i++)
    {
        ave += data[i];
    }
    ave /= n;

    det = 0;
    for (int i = 0; i < n; i++)
    {
        det += (float)pow(data[i] - ave, 2);
    }
    det = (float)sqrt(det / (n - 1));

    degree_of_freedom = n - 1;
    machine = (float)0.02;

    if (degree_of_freedom <= T_TABLE_ROW)
    {
        float t = t_table[degree_of_freedom - 1];
        float uA_std = det / sqrt(n);
        float uA_ext = t * uA_std;
        float uB = machine;
        result = sqrt(uA_ext * uA_ext + uB * uB);

        printf("平均数为：%f\n", ave);
        printf("标准差为：%f\n", det);
        printf("t因子为：%f\n", t);
        printf("A类标准不确定度为：%f\n", uA_std);
        printf("A类扩展不确定度为：%f\n", uA_ext);
        printf("B类不确定度为：%f\n", uB);
        printf("合成不确定度为：%f\n", result);
    }
    else
    {
        printf("自由度超出t分布表范围，无法计算不确定度。\n");
        return 1;
    }

    return 0;
}