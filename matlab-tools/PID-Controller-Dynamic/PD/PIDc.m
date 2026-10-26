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
T=zeros(100,100);
for i=1:1:100
    Kp=i*0.1;
    for j=1:1:100
        Kv=j*0.1;
controller=@(xn,vn)PID_p(xe,xn,vn,Kp,Kv,F_max);
event_func=@(t,x) e(t,x,xe,dx_t,dv_t);
opts=odeset('Events',event_func);
[t,x,te]=ode45(@(t,x)dynamics(t,x,controller,fz,m,g),[0,20],[x0;v0],opts);
if ~isempty(te)
    T(i,j)=te(1);
else
    T(i,j)=20;%位置在时间范围内未收敛时，使用时间长度替代结束时间
end
    end
end
Kp=[0.1:0.1:10];
Kv=[0.1:0.1:10];
figure(1);
surf(Kp,Kv,T);
[min_time, min_idx] = min(T(:));
[kp_p, kv_p] = ind2sub(size(T), min_idx);
Kp=kp_p*0.1;
Kv=kv_p*0.1;
controller=@(xn,vn)PID(xe,xn,vn,Kp,Kv,F_max);
[t,x,te]=ode45(@(t,x)dynamics(t,x,controller,fz,m,g),[0,20],[x0;v0],opts);
figure(2);
plot(t,x(:,1),'r-',t,x(:,2),'b-');
disp(Kp);disp(Kv);disp(te);