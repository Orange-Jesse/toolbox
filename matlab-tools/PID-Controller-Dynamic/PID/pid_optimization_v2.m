% pid_optimization.m
clear;

% 固定参数设置
x0 = 0;       % 初始位置
v0 = 0;       % 初始速度
xe = 5;      % 期望位置
fz = 0.1;     % 阻尼系数
m = 1;        % 无人机质量
g = 10;        % 重力加速度
dx_t = 0.1;   % 位置差值检测阈值
dv_t = 0.1;   % 速度差值检测阈值
F_max = 20;   % 最大出力
disturbance = zeros(1,3); % 正弦干扰
disturbance(1)=false;
disturbance(2)=10;
disturbance(3)=5;

% 定义适应度函数（最小化系统响应时间）
fitnessFunction = @(params) evaluatePID_v2(params, x0, v0, xe, fz, m, g, dx_t, dv_t, F_max, disturbance);

% 参数约束（Kp, Ki, Kd的取值范围）
lb = [0.1, 0.1, 0.1];  % 下界
ub = [50, 50, 50];       % 上界

% 使用optimoptions设置遗传算法参数
options = optimoptions('ga', ...
    'PopulationSize', 50, ...
    'Generations', 80, ...
    'Display', 'iter', ...
    'StallGenLimit', 20, ...
    'PlotFcn', @gaplotbestf);

% 运行遗传算法优化
nvars = 3;  % 优化变量个数 (Kp, Ki, Kd)
[optimalParams, minTime] = ga(fitnessFunction, nvars, [], [], [], [], lb, ub, [], options);

% 输出优化结果
fprintf('最优参数:\n');
fprintf('Kp = %.4f\n', optimalParams(1));
fprintf('Ki = %.4f\n', optimalParams(2));
fprintf('Kd = %.4f\n', optimalParams(3));
fprintf('最小响应时间 = %.4f 秒\n', minTime);

% 使用最优参数进行最终仿真
Kp_opt = optimalParams(1);
Ki_opt = optimalParams(2);
Kd_opt = optimalParams(3);

controller = @(t,x) PID_pi_v2(xe, x, Kp_opt, Ki_opt, Kd_opt, F_max, t);
event_func = @(t,x) e(t, x, xe, dx_t, dv_t);
opts = odeset('Events', event_func);
[t_opt, x_opt, te_opt] = ode45(@(t,x) dynamics_pi(t, x, controller, fz, m, g, disturbance), [0, 20], [x0; v0], opts);

% 绘制最优参数下的系统响应
figure;
plot(t_opt, x_opt(:,1), 'b-', 'LineWidth', 2);
hold on;
plot(t_opt, xe*ones(size(t_opt)), 'r--', 'LineWidth', 1.5);
xlabel('时间 (秒)');
ylabel('位置');
title(sprintf('最优PID参数下的系统响应 (Kp=%.2f, Ki=%.2f, Kd=%.2f)', Kp_opt, Ki_opt, Kd_opt));
legend('实际位置', '期望位置');
grid on;