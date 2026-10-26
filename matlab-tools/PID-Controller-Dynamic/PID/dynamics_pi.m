function dxdt = dynamics_pi(t,x,controller,fz,m,g,disturbance)
%无人机的动力学方程
xn=x(1);
vn=x(2);
if disturbance(1)
    D=@(t)disturbance(2)*sin(disturbance(3)*t);
    D_Value=D(t);
else
    D_Value=0;
end
F=controller(t,x); %调用PID控制器
a=(F/m)-g-(fz*vn/m)+D_Value/m;
dxdt=[vn;a];
end
