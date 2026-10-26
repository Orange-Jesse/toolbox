clear; clc; close all;

% 1. 固定参数定义
I0 = 0;          
R = 1;           
d = 0.1;         
g = 10;          
w0 = 0.1;        
m_plus = 0.1;    
m_dec = -0.02;   
n_objects = 8;   
c = 0.5;         

% 2. 初始状态向量
y0 = zeros(10, 1);       
y0(9) = 0;               
y0(10) = w0;             
y0(1:8) = 0.1;           

% 3. 求解时间区间：延长至60秒（曲线更密集）
tspan = [0, 60];        

% 4. 匿名函数封装
odefun_wrapper = @(t,y) odefun(t, y, I0, R, d, g, m_plus, m_dec, n_objects, c);

% 5. 求解器参数：更小步长（曲线更密集）
options = odeset('RelTol', 1e-3, 'AbsTol', 1e-5, 'MaxStep', 0.05);  

% 6. 刚性求解器ode15s
[t, Y] = ode15s(odefun_wrapper, tspan, y0, options);

% 7. 解码结果
m_result = Y(:, 1:8);          
theta_original = Y(:, 9);      
w_result = Y(:, 10);           

% 8. 极坐标参数
x_polar = sin(theta_original);  
y_polar = cos(theta_original);  
z_polar = t;                    

% 9. 计算角加速度
alpha_result = arrayfun(@(i) (g*R*dot(Y(i,1:8), sin(theta_original(i)+(0:7)*pi/4)) - c*Y(i,10)) / (I0+R^2*sum(Y(i,1:8))), 1:length(t));

% ====================== 窗口1：常规曲线 ======================
fig1 = figure;
set(fig1, 'Color','w', 'Position',[100,0,600,600]); 
% 子图1：8个物体的质量变化
subplot(2,2,1);
plot(t, m_result);
xlabel('Time (s)','FontSize',10); 
ylabel('Mass','FontSize',10);
title('8个物体的质量变化','FontSize',12);
legend({'1','2','3','4','5','6','7','8'},'Location','best','FontSize',8);
grid on;

% 子图2：原始整体角度
subplot(2,2,2);
plot(t, theta_original, 'b-', 'LineWidth', 1);
xlabel('Time (s)','FontSize',10); 
ylabel('Angle (rad)','FontSize',10);
title('原始整体角度','FontSize',12);
grid on;

% 子图3：整体角速度
subplot(2,2,3);
plot(t, w_result);
xlabel('Time (s)','FontSize',10); 
ylabel('Angular velocity (rad/s)','FontSize',10);
title('整体角速度','FontSize',12);
grid on;

% 子图4：整体角加速度
subplot(2,2,4);
plot(t, alpha_result, 'r-', 'LineWidth', 1.5);
xlabel('Time (s)','FontSize',10); 
ylabel('Angular acceleration (rad/s²)','FontSize',10);
title('整体角加速度','FontSize',12);
grid on;

% ====================== 窗口2：极坐标转动图像（压缩时间轴） ======================
fig2 = figure;
set(fig2, 'Color','w', 'Position',[100,0,600,600]); % 正方形窗口，保证XY轴比例正常
plot3(x_polar, y_polar, z_polar, 'Color','k', 'LineWidth', 0.5); % 细黑线

% 标签和样式
xlabel('sin(θ)','FontSize',14); 
ylabel('cos(θ)','FontSize',14); 
zlabel('Time (s)','FontSize',14);
title('角度随时间的极坐标转动','FontSize',16);
grid on; 
set(gca,'FontSize',12); 
% 增强3D效果
light('Position',[5,5,5]); 
lighting gouraud; 