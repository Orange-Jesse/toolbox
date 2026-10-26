I0=0;
R=1;
d=0.1;
g=10;
w0=0.1;
m_plus=0.1;
m_dec=-0.02;

m=zeros(1,2,8);%存储漏斗质量与质量变化率%
I=I0+R^2*sum(m(end,1,:));
theta=zeros(1,3);%存储漏斗角度，角速度和角加速度%
theta(1,2)=w0;

p_m=squeeze(m(end,1,:))';
p_theta=theta(end,1):pi/4:theta(end,1)+pi*7/4;
M=dot(p_m,g*R*sin(p_theta));
theta(end,3)=M/I;

x=R*sin(p_theta);%获取漏斗位置%
y=R*cos(p_theta);

condition = (y > 0) & (x > -d) & (x < d);
m(end,2,condition)=m_plus+m_dec;%注水+漏水%
m(end,2,~condition)=m_dec;%漏水%