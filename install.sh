#!/bin/bash

# Ruma 一键安装脚本
# 支持 Linux (含 Android LXC/Termux)

set -e

echo "========================================"
echo "       Ruma 一键安装脚本"
echo "========================================"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 检查 root 权限
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}请使用 root 权限运行: sudo $0${NC}"
    exit 1
fi

# 检测系统
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    elif [ -f /proc/version ]; then
        if grep -q "Android" /proc/version; then
            echo "android"
        else
            echo "linux"
        fi
    else
        echo "unknown"
    fi
}

OS=$(detect_os)
echo -e "${GREEN}检测到系统: $OS${NC}"

# 安装依赖
install_dependencies() {
    echo -e "${YELLOW}安装系统依赖...${NC}"
    
    if [ "$OS" = "debian" ] || [ "$OS" = "ubuntu" ]; then
        apt-get update
        apt-get install -y curl tar sed grep systemctl nano jq python3 python3-pip python3-venv
    elif [ "$OS" = "android" ]; then
        # Termux 环境
        pkg update
        pkg install -y curl tar sed grep systemctl nano jq python python-pip
    elif [ "$OS" = "arch" ]; then
        pacman -Sy --noconfirm curl tar sed grep systemctl nano jq python python-pip
    fi
    
    echo -e "${GREEN}依赖安装完成${NC}"
}

# 检查 Rurima
check_rurima() {
    echo -e "${YELLOW}检查 Rurima...${NC}"
    
    if command -v rurima &> /dev/null; then
        echo -e "${GREEN}Rurima 已安装: $(which rurima)${NC}"
    else
        echo -e "${RED}Rurima 未安装，请先安装 Rurima${NC}"
        echo "参考: https://github.com/Moe-hacker/rurima"
        exit 1
    fi
}

# 安装 Ruma CLI
install_ruma_cli() {
    echo -e "${YELLOW}安装 Ruma CLI...${NC}"
    
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    
    if [ -f "$SCRIPT_DIR/ruma.sh" ]; then
        cp "$SCRIPT_DIR/ruma.sh" /usr/local/bin/ruma
        chmod +x /usr/local/bin/ruma
        echo -e "${GREEN}Ruma CLI 安装完成${NC}"
    else
        # 从 GitHub 下载
        curl -sL https://raw.githubusercontent.com/xiumuzidiao0/ruma/master/ruma.sh > /usr/local/bin/ruma
        chmod +x /usr/local/bin/ruma
        echo -e "${GREEN}Ruma CLI 安装完成 (从 GitHub 下载)${NC}"
    fi
}

# 安装 Ruma Web UI
install_ruma_web() {
    echo -e "${YELLOW}安装 Ruma Web UI...${NC}"
    
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    
    if [ -f "$SCRIPT_DIR/ruma_web.py" ]; then
        cp "$SCRIPT_DIR/ruma_web.py" /root/ruma_web.py
    else
        # 从 GitHub 下载
        curl -sL https://raw.githubusercontent.com/xiumuzidiao0/ruma/master/ruma_web.py > /root/ruma_web.py
    fi
    
    # 创建虚拟环境
    echo -e "${YELLOW}创建 Python 虚拟环境...${NC}"
    cd /root
    python3 -m venv rumaweb_env
    source rumaweb_env/bin/activate
    
    # 安装依赖
    pip install --upgrade pip -q
    pip install flask psutil -q
    
    echo -e "${GREEN}Web UI 安装完成${NC}"
}

# 配置 Systemd 服务
setup_systemd() {
    echo -e "${YELLOW}配置 Systemd 服务...${NC}"
    
    # 创建启动脚本
    cat > /root/start_ruma_web.sh << 'EOF'
#!/bin/bash
cd /root
source /root/rumaweb_env/bin/activate
exec python ruma_web.py
EOF
    chmod +x /root/start_ruma_web.sh
    
    # 创建服务文件
    cat > /etc/systemd/system/rumaweb.service << 'EOF'
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
EOF
    
    systemctl daemon-reload
    systemctl enable rumaweb
    
    echo -e "${GREEN}Systemd 服务配置完成${NC}"
}

# 初始化 Ruma
init_ruma() {
    echo -e "${YELLOW}初始化 Ruma...${NC}"
    
    # 运行一次 ruma 进行初始化
    echo -e "${YELLOW}请按照提示完成 Ruma 初始化配置...${NC}"
    /usr/local/bin/ruma
}

# 启动服务
start_service() {
    echo -e "${YELLOW}启动 Ruma Web UI...${NC}"
    systemctl start rumaweb
    
    if systemctl is-active --quiet rumaweb; then
        echo -e "${GREEN}Ruma Web UI 已启动${NC}"
    else
        echo -e "${RED}服务启动失败，请检查日志: journalctl -u rumaweb -n 50${NC}"
    fi
}

# 主流程
main() {
    install_dependencies
    check_rurima
    install_ruma_cli
    install_ruma_web
    setup_systemd
    
    echo ""
    echo "========================================"
    echo -e "${GREEN}安装完成!${NC}"
    echo "========================================"
    echo ""
    echo "下一步:"
    echo "  1. 运行 /usr/local/bin/ruma 进行初始化"
    echo "  2. 启动服务: systemctl start rumaweb"
    echo "  3. 访问 Web UI: http://<你的IP>:5777"
    echo ""
    echo "查看 API Key: cat ~/.ruma_config | grep API_KEY"
    echo ""
    
    # 询问是否启动服务
    read -p "是否现在启动 Web UI 服务? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        start_service
    fi
}

# 运行主流程
main
