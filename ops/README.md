# 邮箱容器故障恢复

Docker 的 `restart: unless-stopped` 不会重启仍在运行但 unhealthy 的容器。
此 systemd 定时任务仅检查 `outlook-mail-local`：Docker 已判定 unhealthy 后再持续观察 60 秒，记录健康检查时间、退出码、进程线程数和主机负载，再重启该容器。两次重启尝试至少间隔 10 分钟。healthy、starting、已停止、缺失的容器均不会被启动或重启。systemd 保证同一检查服务不会重叠运行。

这是故障恢复措施，不是阻塞根因修复。检查失败不会触发重启；重启会中断尚未完成的邮箱请求，调用方需重试。日志不记录配置、邮箱、密钥、Cookie 或响应正文。

安装（root）：

```sh
install -d /usr/local/lib/outlook-mail-watchdog
install -m 644 ops/mail_watchdog.py /usr/local/lib/outlook-mail-watchdog/
install -m 644 ops/outlook-mail-watchdog.service ops/outlook-mail-watchdog.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now outlook-mail-watchdog.timer
systemctl start outlook-mail-watchdog.service
```

检查及停用：

```sh
systemctl status outlook-mail-watchdog.timer
journalctl -u outlook-mail-watchdog.service --since today
systemctl disable --now outlook-mail-watchdog.timer
```

验证：`python3 -m unittest discover -s tests -p test_mail_watchdog.py`。测试模拟容器状态，不重启真实服务。
