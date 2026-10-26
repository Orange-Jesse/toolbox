function F = PID_pi(xe,x,Kp_p,Ki_p,Kp_v,Ki_v,F_max,t)
%PID PID控制函数(仅比例项，积分项)
persistent dx_history dv_history t_last;
    dx_history = [];
    dx_i_max=1;
    dv_i_max=1;
    max_history_length = 50;%限制积分历史记录的长度
if isempty(dx_history)
    dx_history = [];
    dv_history = [];
    t_last = 0;
end

dx=xe-x(1);
dt=t-t_last;

if dt>=0
    dx_history=[dx_history;dx*dt];
if length(dx_history) > max_history_length
    dx_history = dx_history(2:end); % 移除最早的记录
end
    R_x=[sum(dx_history),dx_i_max];
    [~,indx]=min(abs(R_x));%钳制积分值
    dx_i=R_x(indx);
    ve=Kp_p*dx+Ki_p*dx_i;
    dv=ve-x(2);
    dv_history=[dv_history;dv*dt];
if length(dv_history) > max_history_length
    dv_history = dv_history(2:end); % 移除最早的记录
end
    R_v=[sum(dv_history),dv_i_max];
    [~,indv]=min(abs(R_v));%钳制积分值
    dv_i=R_v(indv);
end
F =Kp_v*dv+Ki_v*dv_i;

if F>0
    F=min(F_max,F);
else
    F=max(-F_max,F);
end

end