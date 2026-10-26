function [v] = Lashichazhi(x,y,u)
%LASHICHAZHI 此处显示有关此函数的摘要
%   拉氏插值函数
n=length(x);
v=zeros(size(u));
for k = 1:n
    w = ones(size(u));
    for j = [1:k-1 k+1:n]
        w = (u-x(j))./(x(k)-x(j)).*w;
    end
    v = v + w*y(k);
end


