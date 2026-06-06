# 苍坤

Telegram 修仙自动化脚本项目。

## 主要入口

- `intelligent_cultivator.py`: 主号 / 凌霄宫
- `sub_cultivator.py`: 副号 / 星宫
- `cultivator_xiaohao.py`: 小号 / 万灵宗
- `dashboard_server.py`: FastAPI dashboard
- `dashboard.html`: dashboard 前端

## 本地配置

真实配置文件不会提交到 Git：

- `config.json`
- `config_sub.json`

首次部署时从示例复制后填写真实值：

```bash
cp config.example.json config.json
cp config_sub.example.json config_sub.json
```

不要提交 Telegram session、日志、state、cache、bot token 或 VPS 密钥。

## 部署流程

1. 本地修改代码并提交 Git。
2. 同步代码到 VPS `/home/ubuntu/deploy`。
3. 在 VPS 上运行 `python3 -m py_compile` 验证。
4. 重启对应 tmux pane。
5. 将 VPS 上的 state/log/cache 下载回本地覆盖，session 不下载。
