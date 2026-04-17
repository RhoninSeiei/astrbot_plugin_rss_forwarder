# 线上运维

## 基本约束

- 只允许同步当前插件目录。
- 只允许重载 `astrbot_plugin_rss_forwarder`。
- 只允许通过 AstrBot 仪表盘接口执行热重载。
- 禁止重启容器。
- 禁止重载全部插件。

## 常用路径

- 宿主机 SSH：`ssh -p 44012 wty1996@192.168.1.17`
- 插件目录：`/volume1/docker/astrbot/data/plugins/astrbot_plugin_rss_forwarder`
- 配置文件：`/volume1/docker/astrbot/data/config/astrbot_plugin_rss_forwarder_config.json`
- 面板认证配置：`/volume1/docker/astrbot/data/cmd_config.json`
- 状态文件：`/volume1/docker/astrbot/data/plugin_data/astrbot_rss/state.json`

## 单插件热重载

实际使用的是宿主机本地访问 AstrBot 仪表盘接口。

```bash
ssh -p 44012 wty1996@192.168.1.17 "python3 - <<'PY'
import json
import urllib.request
from pathlib import Path

conf = json.loads(
    Path('/volume1/docker/astrbot/data/cmd_config.json').read_text(encoding='utf-8-sig')
)
base = 'http://127.0.0.1:16185/api'

login_req = urllib.request.Request(
    f'{base}/auth/login',
    data=json.dumps({
        'username': conf['dashboard']['username'],
        'password': conf['dashboard']['password'],
    }).encode(),
    headers={'Content-Type': 'application/json'},
)

with urllib.request.urlopen(login_req, timeout=20) as resp:
    token = json.loads(resp.read().decode('utf-8'))['data']['token']

reload_req = urllib.request.Request(
    f'{base}/plugin/reload',
    data=json.dumps({'name': 'astrbot_plugin_rss_forwarder'}).encode(),
    headers={
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    },
)

with urllib.request.urlopen(reload_req, timeout=20) as resp:
    print(resp.read().decode('utf-8'))
PY"
```

正常返回：

```json
{"status":"ok","message":"重载成功。","data":{}}
```

## 代码同步

建议使用打包后传输的方式同步插件目录，避免误碰其他目录：

```bash
cd /mnt/s/Projects/astrbot_plugin_rss_forwarder
tar --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache -cf - . \
  | ssh -p 44012 wty1996@192.168.1.17 \
    "cd /volume1/docker/astrbot/data/plugins/astrbot_plugin_rss_forwarder && tar -xf -"
```

## 配置同步

配置文件在宿主机：

`/volume1/docker/astrbot/data/config/astrbot_plugin_rss_forwarder_config.json`

更新配置后，仍然只执行单插件热重载。

## 发布记录

AstrBot 更新面板优先抓取 GitHub Release。发布版本时需要同时完成：

1. 提交并推送仓库。
2. 创建对应 tag。
3. 发布中文 GitHub Release。
4. 确认 Release 源码包中含有 `CHANGELOG.md`。
