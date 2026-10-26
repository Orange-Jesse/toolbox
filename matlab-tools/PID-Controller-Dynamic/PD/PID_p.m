function F = PID_p(xe,xn,vn,Kp,Kv,F_max)
%PID PID控制函数(仅比例项)
deltax=xn(end)-xe;
ve=-Kp*deltax;
deltav=vn-ve;
F =-Kv*deltav;
if F>0
    F=min(F_max,F);
else
    F=max(-F_max,F);
end
end