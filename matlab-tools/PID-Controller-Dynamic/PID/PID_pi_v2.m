function F = PID_pi_v2(xe,x,Kp,Ki,Kd,F_max,t)
%PID PID控制函数(仅比例项，积分项)
persistent dx_history t_last;
    dx_history = [];
    dx_i_max=100;
    max_history_length = 50;%限制积分历史记录的长度
if isempty(dx_history)
    dx_history = [];
    t_last = 0;
end

dx=xe-x(1);
dt=t-t_last;
t_last=t;

if dt>=0
    dx_history=[dx_history;dx*dt];
if length(dx_history) > max_history_length
    dx_history = dx_history(2:end); % 移除最早的记录
end
    R_x=[sum(dx_history),dx_i_max];
    [~,indx]=min(abs(R_x));%钳制积分值
    dx_i=R_x(indx);
end

dv=0-x(2);
F =Kp*dx+Ki*dx_i+Kd*dv;
if F>0
    F=min(F_max,F);
else
    F=max(-F_max,F);
end

end