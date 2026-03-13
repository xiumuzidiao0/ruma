# Ruma - Ruri Manager Wrapper

Ruma 是一个基于 [Rurima](https://github.com/Moe-hacker/rurima) 的轻量级容器管理工具，旨在简化在 Linux (包括 Android Termux/LXC) 环境下部署和管理 Linux 容器的流程。

它包含两个主要组件：
1. Ruma CLI (`ruma.sh`): 一个功能强大的 Bash 脚本，封装了 Rurima 的底层命令，提供了类似 Docker CLI 的体验，并集成了 Systemd 服务管理。
2. Ruma Web UI (`ruma_web.py`): 一个基于 Python Flask 的现代化 Web 管理面板，提供图形化界面来管理容器、文件、终端和任务。

## ✨ 主要特性

### 🖥 Ruma CLI
* 自动化部署: 自动拉取镜像、解析启动命令、生成 Systemd 服务文件。
* 服务管理: 使用 systemctl 管理容器生命周期 (Start/Stop/Restart/Enable)。
* 镜像加速: 内置 Docker 镜像加速支持 (默认 docker.1ms.run)，支持开关配置。
* 数据持久化: 支持挂载宿主机目录 (-v)，自动检测并创建不存在的挂载源。
* 备份与恢复: 一键备份容器为 tar 包，或从备份恢复。
* 导入导出: 支持导入 docker save 格式的镜像包（自动合并层）和 docker export 包。
* 交互式/命令行模式: 支持向导式部署或单行命令操作。

### 🌐 Ruma Web UI
* 仪表盘: 查看容器状态、端口映射（支持点击跳转）。
* Web 终端: 内置基于 xterm.js 和 Python pty 的全功能终端，无需额外依赖即可连接容器 Shell。
* 文件管理: 在线浏览、编辑容器内的文件。
* 模板部署: 支持使用 YAML 风格的模板快速部署复杂应用，支持保存和管理模板库。
* 本地导入: 上传 tar 镜像包，自动解析 Config (Cmd/Env/WorkDir) 并部署。
* 任务队列: 异步处理拉取、导入等耗时操作，实时查看日志进度，支持任务历史记录。
* 定时备份: 图形化配置 Cron 定时备份任务。

## 🛠 安装与依赖

### 依赖
* 系统: Linux (支持 Systemd), Android (需 Root 或 LXC 环境)。
* 核心: rurima (需预先安装并配置好)。
* CLI: bash, curl, tar, sed, grep, systemctl, nano, `jq`。
* Web UI: `python3`, pip (安装 flask)。

### 安装步骤

#### 方式一: 一键安装 (推荐)

```bash
# 下载并运行安装脚本
curl -sL https://raw.githubusercontent.com/xiumuzidiao0/ruma/master/install.sh | sudo bash
```

或下载项目后本地运行:
```bash
chmod +x install.sh
sudo ./install.sh
```

#### 方式二: 手动安装

1. 安装 Rurima: 请参考 Rurima 项目 进行安装。
2. 安装 Ruma CLI:
 将 ruma.sh 复制到系统路径并赋予执行权限：
 cp ruma.sh /usr/local/bin/ruma
 chmod +x /usr/local/bin/ruma
 
3. 安装 Web UI:
 安装 Python 依赖：
 pip3 install flask psutil
 
 启动 Web 服务：
 python3 ruma_web.py
 
 *(建议配置为 Systemd 服务以实现开机自启)*

### Systemd 服务配置 (可选)

创建 `/etc/systemd/system/rumaweb.service`:

```ini
[Unit]
Description=Ruma Web Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/root/start_ruma_web.sh
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

创建启动脚本 `/root/start_ruma_web.sh`:

```bash
#!/bin/bash
cd /root
source /root/rumaweb_env/bin/activate
exec python ruma_web.py
```

然后启用服务：
```bash
chmod +x /root/start_ruma_web.sh
systemctl daemon-reload
systemctl enable rumaweb
systemctl start rumaweb
```

## 📖 CLI 使用指南 (ruma)

### 第一次运行（重要）

第一次运行需要在终端中运行一次
ruma
进行初始化，需手动设置一些参数和安装路径

直接运行 ruma 进入交互式部署向导，或使用子命令：

| 命令 | 说明 | 示例 |
| :--- | :--- | :--- |
| run | 部署新容器 | ruma run -v /opt:/data --name myapp alpine |
| ps | 列出所有容器 | ruma ps |
| start/stop/restart | 管理容器状态 | ruma restart myapp |
| rm | 删除容器及服务 | ruma rm myapp |
| logs | 查看容器日志 | ruma logs myapp |
| exec | 进入容器终端 | ruma exec myapp /bin/sh |
| backup | 备份容器 | ruma backup myapp |
| restore | 恢复容器 | ruma restore myapp /path/to/backup.tar |
| update | 更新容器镜像 | ruma update myapp |
| import | 导入本地镜像 | ruma import -f image.tar -n myapp |

部署示例:
ruma run --name alist \
 -v /opt/alist:/opt/alist/data \
 -e PUID=0 -e PGID=0 \
 --autorun y \
 xhofe/alist:latest

## 🖥 Web UI 使用指南

1. 访问: 浏览器打开 `http://<服务器IP>:5777`。
2. 登录: 首次启动时，API Key 会自动生成并保存在 ~/.ruma_config 文件中。
 * 查看 Key: cat ~/.ruma_config | grep API_KEY
3. 功能亮点:
 * 部署: 点击"部署容器"，选择"拉取镜像"或"本地导入"。
 * 本地导入: 上传 tar 包后点击"解析"，系统会自动提取启动命令，工作目录和环境变量。
 * 模板: 点击"使用模板"，输入类似以下的配置：
 -name: my-web
 -image: nginx:latest
 -mirror: docker.1ms.run
 -v /var/www:/usr/share/nginx/html
 -e TZ:Asia/Shanghai
 
 * 管理: 在容器列表中点击"管"理按钮，可以：
 * 修改容器名称（自动重命名服务和目录）。
 * 修改启动命令，工作目录、环境变量。
 * 浏览和编辑容器内文件。
 * 连接 Web 终端。
 * 管理备份和 Cron 任务。

## ⚙️ 配置文件

配置文件位于 `~/.ruma_config`，由脚本自动维护，包含以下内容：
* `RURIMA_BIN`: rurima 二进制路径。
* DEFAULT_CONTAINER_ROOT`: 容器默认存储路径。
* `DOCKER_MIRROR`: 默认镜像加速源。
* `USE_MIRROR`: 是否启用镜像加速 ("true"/"false")。
* `API_KEY`: Web UI 访问密钥。
* `CONTAINER|...`: 已注册容器的数据库记录。

## ⚠️ 注意事项

* Docker Save vs Export:
 * docker save 导出的 tar 包包含 manifest.json 和分层数据，Ruma 支持自动解析配置并合并层。
 * docker export 导出的 tar 包仅包含 rootfs，不含配置，导入时需手动指定启动命令。
* 架构兼容性: Rurima 会检查镜像架构。如果在 ARM 设备上拉取 x86 镜像可能会失败，除非使用 -f 强制模式（但通常无法运行）。

---
*Powered by Rurima & Ruma*
