clear;
Kp=1.5;%位置差值-期望速度 比例系数
Kv=10;%速度差值-期望出力 比例系数
x0=0;%初始位置
v0=0;%初始速度
xe=50;%期望位置
fz=0.1;%阻尼系数
m=1;%无人机质量
g=0;%重力加速度
dx_t=0.1;%位置差值检测阈值
dv_t=0.1;%速度差值检测阈值
F_max=50;%最大出力
controller=@(xn,vn)PID_p(xe,xn,vn,Kp,Kv,F_max);
event_func=@(t,x) e(t,x,xe,dx_t,dv_t);
opts=odeset('Events',event_func);
[t,x,te]=ode45(@(t,x)dynamics(t,x,controller,fz,m,g),[0,20],[x0;v0],opts);
figure(1);
plot(t,x(:,1),'r-',t,x(:,2),'b-');
disp(Kp);disp(Kv);disp(te);