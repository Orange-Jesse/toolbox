function [value, isterminal, direction] = e(t, x, xe, dx_t, dv_t)
    dx = abs(x(1) - xe);
    dv = abs(x(2));
    
    % 当两个误差都小于阈值时，value从正变负
    pos_met = dx_t - dx;    % 位置误差达标时为正
    vel_met = dv_t - dv;    % 速度误差达标时为正
    
    % 当两者同时为正时，min返回正值
    % 当至少一个不达标时，min返回负值
    value = -(min(pos_met, vel_met) - 1e-6);  % 小余量防止数值误差
    
    isterminal = 0;
    direction = [];
end
