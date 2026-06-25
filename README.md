# 凡人修仙记

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

## 必要工作流程

真实运行环境在 VPS：`ubuntu@VPS_HOST:/home/ubuntu/deploy`。本地改完不代表线上已生效。

1. 修改前查看 `git status --short --branch`，必要时先查 VPS 日志或 state。
2. 本地修改代码，优先复用通用模块。
3. 本地运行 `python -m py_compile ...` 和相关测试。
4. 用 `scp -O -i C:\path\to\vps-key.pem` 上传改动文件到 VPS。
5. 在 VPS 用 `/home/ubuntu/deploy/venv/bin/python -m py_compile ...` 复查。
6. 重启对应 tmux：`xiuxian:0` 主号，`xiuxian:1` 副号，`xiuxian:2` 小号，`xiuxian:3` dashboard。
7. 检查 VPS 进程、tmux、日志或 dashboard，确认线上已生效。
8. 将 VPS 上的 `state_*.json` 和 `*.log` 下载回本地覆盖，session 不下载。
9. 只 stage 本次相关文件，commit 并 push；如果不能提交或推送，必须明确说明原因。

SSH/SCP 必须显式带 key：

```powershell
ssh -i C:\path\to\vps-key.pem ubuntu@VPS_HOST
scp -O -i C:\path\to\vps-key.pem .\changed_file.py ubuntu@VPS_HOST:/home/ubuntu/deploy/
```
