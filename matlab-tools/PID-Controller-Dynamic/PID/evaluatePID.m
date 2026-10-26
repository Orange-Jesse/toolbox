% evaluatePID.m
function responseTime = evaluatePID(params, x0, v0, xe, fz, m, g, dx_t, dv_t, F_max, disturbance)
    % 从参数向量中提取PID参数
    Kp_x = params(1);
    Ki_x = params(2);
    Kp_v = params(3);
    Ki_v = params(4);
    
    % 定义控制器
    controller = @(t,x) PID_pi(xe, x, Kp_x, Ki_x, Kp_v, Ki_v, F_max, t);
    
    % 定义事件函数（检测系统是否达到稳态）
    event_func = @(t,x) e(t, x, xe, dx_t, dv_t);
    
    % 设置ODE求解器选项
    opts = odeset('Events', event_func);
    
    % 求解系统动力学方程
    try
        [~, ~, te] = ode45(@(t,x) dynamics_pi(t, x, controller, fz, m, g, disturbance), [0, 20], [x0; v0], opts);
        
        % 如果系统在时间范围内达到稳态，返回收敛时间
        if ~isempty(te)
            responseTime = te(1);
        else
            % 否则返回最大时间（惩罚未收敛的参数组合）
            responseTime = 20;
        end
    catch
        % 处理可能的数值问题（如不稳定导致的求解失败）
        responseTime = 20;
    end
end