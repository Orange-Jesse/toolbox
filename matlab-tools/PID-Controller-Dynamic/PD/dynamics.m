function dxdt = dynamics(t,x,controller,fz,m,g)
%无人机的动力学方程
xn=x(1);
vn=x(2);
F=controller(xn,vn); %调用PID控制算法
a=(F/m)-g-(fz*vn/m);
dxdt=[vn;a];
end

