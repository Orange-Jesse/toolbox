function dydt = odefun(t, y, I0, R, d, g, m_plus, m_dec, n_objects, c)
    % 1. 解码状态向量（列向量，无冗余转置）
    m = y(1:n_objects);          % 8×1列向量
    theta = y(n_objects+1);      % 当前角度θ（标量）
    w = y(n_objects+2);          % 当前角速度ω（标量）

    % 2. 计算8个物体的角度/坐标（快速生成）
    p_theta = theta + (0:7)*pi/4;% 1×8行向量，8个角度
    x = R * sin(p_theta);        % 1×8行向量
    y_pos = R * cos(p_theta);    % 1×8行向量
    
    % 3. 质量更新条件（向量化）
    condition = (y_pos > 0) & (x > -d);  % 1×8逻辑行向量
    
    % 4. 计算质量变化率（修复下限逻辑，无突变）
    dm = zeros(n_objects, 1);    % 8×1列向量（匹配m的维度）
    dm(condition) = m_plus + m_dec;  % 满足条件的质量变化率
    dm(~condition) = m_dec;          % 不满足条件的质量变化率
    % 正确限制质量下限：变化率平滑过渡到0.2
    m_next = m + dm * 1e-3;      
    idx = m_next < 0.1;
    dm(idx) = (0.1 - m(idx)) / 1e-3;  

    % 5. 计算转动惯量（无下限）
    I = I0 + R^2 * sum(m);  

    % 6. 计算力矩+角加速度（修复矩阵乘法错误：改用dot或按元素乘求和）
    M = g*R * dot(m, sin(p_theta)) - c * w;  % 用dot计算点积，兼容列/行向量
    alpha = M / I;  

    % 7. 组装状态导数向量（无转置，直接赋值）
    dydt = zeros(size(y));  % 10×1列向量
    dydt(1:n_objects) = dm;       % 8×1 → 直接赋值，无维度问题
    dydt(n_objects+1) = w;         % dθ/dt = 角速度
    dydt(n_objects+2) = alpha;     % dω/dt = 角加速度
end