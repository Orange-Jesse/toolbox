clear;
Kp_p=1;%位置差值-期望速度 比例系数
Ki_p=1;%速度差值-期望出力 比例系数
Kp_v=1;%位置差值-期望出力 积分系数
Ki_v=1;%速度差值-期望出力 积分系数
x0=0;%初始位置
v0=0;%初始速度
xe=20;%期望位置
fz=0.1;%阻尼系数
m=1;%无人机质量
g=0;%重力加速度
dx_t=0.1;%位置差值检测阈值
dv_t=0.1;%速度差值检测阈值
F_max=20;%最大出力
disturbance=zeros(1,3);%正弦干扰
controller=@(t,x)PID_pi(xe,x,Kp_p,Ki_p,Kp_v,Ki_v,F_max,t);
event_func=@(t,x)e(t,x,xe,dx_t,dv_t);
opts=odeset('Events',event_func);
[t,x,te]=ode45(@(t,x)dynamics_pi(t,x,controller,fz,m,g,disturbance),[0,20],[x0;v0],opts);
figure;
plot(t,x(:,1),'r-',t,x(:,2),'b-');
disp(Kp_p);disp(Ki_p);disp(te);