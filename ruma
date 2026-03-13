#!/bin/bash

# =================================================================
# Ruma - Ruri Manager Wrapper (v5.1)
# 简化 Ruri 容器部署流程
# 更新日志: v5.2 新增自定义 Docker 镜像加速源配置 (默认 docker.1ms.run)
# =================================================================

RUMA_VERSION="5.2"
CONFIG_FILE="$HOME/.ruma_config"
CURRENT_DIR=$(pwd)
DEFAULT_BACKUP_DIR="$HOME/ruma_backups"

# --- 颜色定义 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

get_abs_path() {
    local path="$1"
    if [[ "$path" == /* ]]; then echo "$path"; else echo "$CURRENT_DIR/$path"; fi
}

# --- 检查并创建挂载源 ---
check_and_create_mount_src() {
    local src="$1"
    if [ -e "$src" ]; then return; fi
    
    local filename=$(basename "$src")
    # 简单的启发式判断：如果有后缀名则视为空文件，否则视为目录
    if [[ "$filename" == *.* ]]; then
        echo -e "${YELLOW}[自动创建] 挂载源文件不存在: $src (创建为空文件, 权限777)${NC}" >&2
        mkdir -p "$(dirname "$src")"
        touch "$src"
        chmod 777 "$src"
    else
        echo -e "${YELLOW}[自动创建] 挂载源目录不存在: $src (创建为目录, 权限777)${NC}" >&2
        mkdir -p "$src"
        chmod 777 "$src"
    fi
}

# --- 0. 环境依赖检查 ---
check_dependencies() {
    local deps=("curl" "tar" "sed" "grep" "systemctl" "nano" "jq")
    local missing=()
    for cmd in "${deps[@]}"; do
        if ! command -v "$cmd" &> /dev/null; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -ne 0 ]; then
        echo -e "${RED}错误: 缺少必要依赖工具: ${missing[*]}${NC}"
        echo "请先安装它们 (例如: apt install ${missing[*]})"
        exit 1
    fi
    if [ "$EUID" -ne 0 ]; then
        echo -e "${RED}错误: Ruma 需要 root 权限运行。${NC}"
        exit 1
    fi
}

# --- 1. 初始化配置 ---
init_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        if [[ "$1" == "check_only" ]]; then return; fi
        
        echo -e "${YELLOW}[初始化] 检测到首次运行，请配置基本信息。${NC}"
        while true; do
            read -p "请输入 rurima 安装目录 (如 /usr/local/bin/rurima): " input_bin
            if [ -z "$input_bin" ]; then input_bin=$(which rurima 2>/dev/null); fi
            if [ -n "$input_bin" ] && [ -x "$input_bin" ]; then
                RURIMA_BIN="$input_bin"
                echo -e "${GREEN}已确认 rurima 路径: $RURIMA_BIN${NC}"
                break
            else
                echo -e "${RED}错误: 找不到该文件或不可执行。${NC}"
            fi
        done
        read -p "请设置容器安装默认目录 (如 /root/container): " input_root
        if [ -z "$input_root" ]; then input_root="/root/container"; fi
        
        # 新增：镜像加速源配置
        read -p "请设置默认 Docker 镜像加速源 (默认 docker.1ms.run): " input_mirror
        if [ -z "$input_mirror" ]; then input_mirror="docker.1ms.run"; fi

        mkdir -p "$input_root"
        DEFAULT_CONTAINER_ROOT=$(get_abs_path "$input_root")
        echo -e "${GREEN}已设置默认容器目录: $DEFAULT_CONTAINER_ROOT${NC}"
        
        echo "RURIMA_BIN=\"$RURIMA_BIN\"" > "$CONFIG_FILE"
        echo "DEFAULT_CONTAINER_ROOT=\"$DEFAULT_CONTAINER_ROOT\"" >> "$CONFIG_FILE"
        echo "DOCKER_MIRROR=\"$input_mirror\"" >> "$CONFIG_FILE"
        echo "USE_MIRROR=\"true\"" >> "$CONFIG_FILE"
        echo "------------------------------------------------"
    fi
    if [ -f "$CONFIG_FILE" ]; then source <(grep -v "^CONTAINER|" "$CONFIG_FILE"); fi
    # 确保变量有默认值 (兼容旧版配置)
    if [ -z "$DOCKER_MIRROR" ]; then DOCKER_MIRROR="docker.1ms.run"; fi
    if [ -z "$USE_MIRROR" ]; then USE_MIRROR="true"; fi
    mkdir -p "$DEFAULT_BACKUP_DIR"
}

# --- 数据库操作 ---
save_record() {
    local name="$1"; local img="$2"; local path="$3"; local svc="$4"
    if [ -f "$CONFIG_FILE" ]; then sed -i "/^CONTAINER|$name|/d" "$CONFIG_FILE"; fi
    echo "CONTAINER|$name|$img|$path|$svc" >> "$CONFIG_FILE"
}

delete_record() {
    local name="$1"
    if [ -f "$CONFIG_FILE" ]; then sed -i "/^CONTAINER|$name|/d" "$CONFIG_FILE"; fi
}

# --- 2. 解析挂载 ---
parse_mounts() {
    local input_str="$1"; local result_str=""
    if [ -z "$input_str" ]; then return; fi
    IFS=',' read -ra ADDR <<< "$input_str"
    for i in "${ADDR[@]}"; do
        i=$(echo "$i" | xargs)
        if [[ "$i" == *":"* ]]; then src="${i%%:*}"; tgt="${i#*:}"; else read -r src tgt <<< "$i"; fi
        src=$(get_abs_path "$src")
        if [ -n "$src" ] && [ -n "$tgt" ]; then result_str="$result_str -m $src $tgt"; fi
    done
    echo "$result_str"
}

# --- 3. 解析环境变量 ---
parse_envs() {
    local input_str="$1"; local result_str=""
    if [ -z "$input_str" ]; then return; fi
    IFS=',' read -ra ADDR <<< "$input_str"
    for i in "${ADDR[@]}"; do
        i=$(echo "$i" | xargs)
        if [[ "$i" == *"="* ]]; then k="${i%%=*}"; v="${i#*=}"; else read -r k v <<< "$i"; fi
        if [ -n "$k" ] && [ -n "$v" ]; then result_str="$result_str -e $k $v"; fi
    done
    echo "$result_str"
}

# --- 4. 核心功能：部署容器 ---
deploy_container() {
    local raw_img_name="$1"
    local install_path="$2"
    local mount_args="$3"
    local env_args="$4"
    local auto_start="$5"
    local cli_extra_mounts="$7"
    local cli_extra_envs="$8"
    local custom_name="$9"

    local tmp_svc_name="${raw_img_name##*/}"
    local short_svc_name="${tmp_svc_name%%:*}"
    local service_name="${custom_name:-$short_svc_name}"
    local service_file="/etc/systemd/system/${service_name}.service"

    # === 镜像源处理逻辑 ===
    local final_img_name="$raw_img_name"
    local mirror_flag=""

    # 检查是否包含自定义域名 (如 ghcr.io/xxx, docker.1ms.run/xxx)
    # 判断依据: 第一部分是否包含 "."
    if [[ "$raw_img_name" =~ ^[^/]+\.[^/]+/ ]]; then
        local registry_domain="${raw_img_name%%/*}"
        local remainder="${raw_img_name#*/}"
        echo -e "${YELLOW}检测到完整镜像路径: $registry_domain${NC}"
        mirror_flag="-m $registry_domain"
        final_img_name="$remainder"
    else
        # 没有域名，说明是 DockerHub 官方镜像，应用默认加速源
        if [ "$USE_MIRROR" == "false" ]; then
            echo -e "${YELLOW}镜像加速已关闭。${NC}"
            mirror_flag=""
            final_img_name="$raw_img_name"
        else
            echo -e "${YELLOW}使用配置的加速源: $DOCKER_MIRROR${NC}"
            mirror_flag="-m $DOCKER_MIRROR"
            final_img_name="$raw_img_name"
        fi
    fi

    echo -e "${BLUE}[1/4] 开始拉取镜像...${NC}"
    echo "执行: $RURIMA_BIN pull $mirror_flag $final_img_name $install_path"
    local pull_log_tmp=$(mktemp)
    "$RURIMA_BIN" pull $mirror_flag "$final_img_name" "$install_path" 2>&1 | tee "$pull_log_tmp"
    pull_output=$(cat "$pull_log_tmp"); rm -f "$pull_log_tmp"

    echo -e "${BLUE}[2/4] 解析启动命令...${NC}"
    raw_cmd=$(echo "$pull_output" | grep -E "rurima r .* /" | tail -n 1)
    
    if [ -z "$raw_cmd" ]; then
        echo "------------------------------------------------"
        echo -e "${RED}错误: 未能捕获启动命令，流程终止。${NC}"
        return 1
    fi

    clean_cmd=$(echo "$raw_cmd" | sed -r "s/\x1B\[([0-9]{1,2}(;[0-9]{1,2})?)?[mGK]//g")
    clean_cmd=$(echo "$clean_cmd" | sed -E 's/-e "PS1" "[^"]*" //g')
    clean_cmd=$(echo "$clean_cmd" | sed -E 's/-e "TERM" "[^"]*" //g')

    args_part=$(echo "$clean_cmd" | sed -n 's/.*rurima r \(.*\)/\1/p')
    if [ -z "$args_part" ]; then args_part=$(echo "$clean_cmd" | sed -n 's/.*ruri \(.*\)/\1/p'); fi
    
    local captured_pre_args=${args_part%%$install_path*}
    local captured_post_args=${args_part#*$install_path}
    
    if [ -z "$captured_post_args" ] || [ "$captured_post_args" == "$args_part" ]; then 
        captured_post_args=" /init"
    fi

    ensure_dns_config() {
        local dns_file="/root/resolv.conf"
        if [ ! -f "$dns_file" ]; then
            echo -e "${YELLOW}DNS文件不存在，正在创建 /root/resolv.conf ...${NC}" >&2
            echo -e "nameserver 223.5.5.5\nnameserver 8.8.8.8\nnameserver 114.114.114.114" > "$dns_file"
        fi
        
        if [[ "$mount_args" != *"/etc/resolv.conf"* && "$cli_extra_mounts" != *"/etc/resolv.conf"* ]]; then
            echo -e "${GREEN}自动注入 DNS 配置: -M $dns_file /etc/resolv.conf${NC}" >&2
            echo "-M $dns_file /etc/resolv.conf"
        else
            echo ""
        fi
    }
    
    local auto_dns_mount=$(ensure_dns_config)

    echo -e "捕获参数前缀 (已净化): $captured_pre_args"

    echo -e "${BLUE}[3/4] 配置并启动服务...${NC}"

    local unshare_flag_str="-u"
    
    local final_exec="$RURIMA_BIN r $unshare_flag_str $auto_dns_mount $captured_pre_args $mount_args $cli_extra_mounts $env_args $cli_extra_envs $install_path $captured_post_args"
    final_exec=$(echo "$final_exec" | tr -s ' ')

    echo -e "ExecStart命令: $final_exec"

    cat > "$service_file" <<EOF
[Unit]
Description=Rurima Container - $service_name
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/root
ExecStart=$final_exec
TimeoutStopSec=30
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

    echo "注册服务..."
    systemctl daemon-reload
    if [[ "$auto_start" =~ ^[yY] ]]; then
        echo -e "${GREEN}已启用开机自启。${NC}"
        systemctl enable "$service_name" >/dev/null 2>&1
    else
        echo -e "${YELLOW}未启用开机自启。${NC}"
        systemctl disable "$service_name" >/dev/null 2>&1
    fi
    
    echo "启动服务..."
    systemctl stop "$service_name" 2>/dev/null
    systemctl start "$service_name"
    sleep 2

    # 记录时使用包含域名的完整镜像名，方便后续识别
    local record_img_name="$raw_img_name"
    if [[ ! "$raw_img_name" =~ ^[^/]+\.[^/]+/ ]]; then
        record_img_name="${DOCKER_MIRROR}/${raw_img_name}"
    fi

    if systemctl is-active --quiet "$service_name"; then
        echo -e "${GREEN}SUCCESS! 服务 $service_name 已成功启动。${NC}"
        save_record "$service_name" "$record_img_name" "$install_path" "$service_name"
        echo -e "${BLUE}容器信息已记录到 $CONFIG_FILE${NC}"
    else
        echo -e "${RED}ERROR: 服务启动失败。请检查日志: journalctl -u $service_name${NC}"
    fi
}

# --- 5. 列出容器 ---
show_ps() {
    echo -e "${BLUE}Ruma 容器列表:${NC}"
    printf "${YELLOW}%-20s %-10s %-30s %s${NC}\n" "NAME" "STATUS" "IMAGE" "PATH"
    echo "--------------------------------------------------------------------------------"
    if [ ! -f "$CONFIG_FILE" ]; then return; fi
    grep "^CONTAINER|" "$CONFIG_FILE" | while IFS='|' read -r _ name img path svc; do
        if systemctl is-active --quiet "$svc"; then
            status="${GREEN}RUNNING${NC}"
        else
            status="${RED}STOPPED${NC}"
        fi
        printf "%-20s %-19s %-30s %s\n" "$name" "$status" "$img" "$path"
    done
}

# --- 6. 管理服务 ---
manage_service() {
    local action="$1"; local target_name="$2"
    if [ -z "$target_name" ]; then echo "用法: ruma $action [容器名]"; show_ps; exit 1; fi
    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"

    case "$action" in
        start)
            echo -e "${BLUE}正在启动容器 $name ...${NC}"
            systemctl start "$svc"; sleep 1
            if systemctl is-active --quiet "$svc"; then echo -e "${GREEN}启动成功!${NC}"; else echo -e "${RED}启动失败!${NC}"; fi ;;
        stop)
            echo -e "${BLUE}正在停止容器 $name ...${NC}"
            systemctl stop "$svc"
            if ! systemctl is-active --quiet "$svc"; then echo -e "${GREEN}停止成功!${NC}"; else echo -e "${RED}停止失败!${NC}"; fi ;;
        restart)
            echo -e "${BLUE}正在重启容器 $name ...${NC}"
            systemctl restart "$svc"; sleep 1
            if systemctl is-active --quiet "$svc"; then echo -e "${GREEN}重启成功!${NC}"; else echo -e "${RED}重启失败!${NC}"; fi ;;
    esac
}

# --- 7. 删除容器 ---
remove_container() {
    local target_name="$1"
    if [ -z "$target_name" ]; then echo "用法: ruma rm [容器名]"; show_ps; exit 1; fi
    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"
    local auto_confirm="$2"
    if [[ "$auto_confirm" != "-y" ]]; then
        echo -e "${YELLOW}警告: 即将删除容器: $name${NC}"
        read -p "确定要继续吗? (y/n): " confirm
        if [[ ! "$confirm" =~ ^[yY] ]]; then echo "操作已取消"; return; fi
    fi

    echo -e "${BLUE}停止服务...${NC}"
    systemctl stop "$svc" 2>/dev/null
    systemctl disable "$svc" 2>/dev/null
    rm "/etc/systemd/system/${svc}.service" 2>/dev/null
    systemctl daemon-reload

    echo -e "${BLUE}删除文件...${NC}"
    umount -l "$path" 2>/dev/null
    if [ -f "$path/.rurienv" ]; then chattr -i "$path/.rurienv" 2>/dev/null; fi
    rm -rf "$path"

    delete_record "$name"
    echo -e "${GREEN}删除完成!${NC}"
}

# --- 8. 备份容器 ---
backup_container() {
    local target_name="$1"
    local skip_restart="$2" # 内部参数，用于 update 流程
    if [ -z "$target_name" ]; then echo "用法: ruma backup [容器名]"; show_ps; exit 1; fi
    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"

    local timestamp=$(date +%Y%m%d_%H%M%S)
    local backup_file="$DEFAULT_BACKUP_DIR/${name}_${timestamp}.tar.gz"

    echo -e "${YELLOW}正在备份容器: $name${NC}"
    echo -e "目标文件: $backup_file"
    
    echo -e "${BLUE}[1/2] 停止服务...${NC}"
    systemctl stop "$svc"
    sleep 2
    umount -l "$path" 2>/dev/null

    echo -e "${BLUE}[2/2] 打包文件...${NC}"
    tar -czpf "$backup_file" --numeric-owner -C "$path" .
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}备份成功!${NC}"
    else
        echo -e "${RED}备份失败!${NC}"
        return 1
    fi

    if [ "$skip_restart" != "no_restart" ]; then
        echo "恢复服务..."
        systemctl start "$svc"
    fi
}

# --- 9. 恢复容器 ---
restore_container() {
    local target_name="$1"; local backup_file="$2"
    if [ -z "$target_name" ] || [ -z "$backup_file" ]; then echo "用法: ruma restore [容器名] [备份路径]"; exit 1; fi
    if [ ! -f "$backup_file" ]; then echo -e "${RED}错误: 文件不存在${NC}"; exit 1; fi

    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器记录。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"

    echo -e "${RED}警告: 即将清空并恢复容器: $name${NC}"
    read -p "确定要继续吗? (y/n): " confirm
    if [[ ! "$confirm" =~ ^[yY] ]]; then echo "操作已取消"; return; fi

    echo -e "${BLUE}停止并清理...${NC}"
    systemctl stop "$svc"
    umount -l "$path" 2>/dev/null
    if [ -f "$path/.rurienv" ]; then chattr -i "$path/.rurienv" 2>/dev/null; fi
    find "$path" -mindepth 1 -delete 2>/dev/null
    mkdir -p "$path"

    echo -e "${BLUE}解压备份...${NC}"
    tar -xzpf "$backup_file" --numeric-owner -C "$path"
    
    echo -e "${BLUE}重启服务...${NC}"
    systemctl start "$svc"
    if systemctl is-active --quiet "$svc"; then echo -e "${GREEN}恢复成功!${NC}"; else echo -e "${RED}启动失败。${NC}"; fi
}

# --- 10. 更新容器 (Update) ---
update_container() {
    local target_name="$1"
    if [ -z "$target_name" ]; then echo "用法: ruma update [容器名]"; show_ps; exit 1; fi
    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"

    echo -e "${YELLOW}=== 容器更新向导: $name ===${NC}"
    echo -e "镜像源: $img"
    
    # 智能解析镜像源
    local mirror_flag=""
    local final_img_name="$img"
    
    # 如果记录的镜像名没有域名，自动加上默认镜像源
    if [[ ! "$img" =~ ^[^/]+\.[^/]+/ ]]; then
        if [ "$USE_MIRROR" == "false" ]; then
            echo -e "${YELLOW}镜像加速已关闭。${NC}"
            mirror_flag=""
        else
            echo -e "${YELLOW}使用默认镜像源: $DOCKER_MIRROR${NC}"
            mirror_flag="-m $DOCKER_MIRROR"
        fi
    elif [[ "$img" =~ ^[^/]+\.[^/]+/ ]]; then
        # 如果有域名，检查是否启用镜像加速
        if [ "$USE_MIRROR" == "false" ]; then
            echo -e "${YELLOW}镜像加速已关闭，使用原始镜像源。${NC}"
            mirror_flag=""
            final_img_name="$img"
        else
            # 如果有域名，解析出 mirror 和 name
            local registry_domain="${img%%/*}"
            local remainder="${img#*/}"
            mirror_flag="-m $registry_domain"
            final_img_name="$remainder"
        fi
    fi

    local auto_confirm="$2"
    if [[ "$auto_confirm" != "-y" ]]; then
        read -p "确定要更新吗? (y/n): " confirm
        if [[ ! "$confirm" =~ ^[yY] ]]; then echo "操作已取消"; return; fi
    fi

    backup_container "$name" "no_restart"
    if [ $? -ne 0 ]; then echo -e "${RED}自动备份失败，终止更新。${NC}"; return; fi

    echo -e "${BLUE}[更新] 拉取最新镜像...${NC}"
    "$RURIMA_BIN" pull $mirror_flag "$final_img_name" "$path"
    
    echo -e "${BLUE}[更新] 重启服务...${NC}"
    systemctl start "$svc"
    sleep 2
    if systemctl is-active --quiet "$svc"; then echo -e "${GREEN}SUCCESS! 更新完成。${NC}"; else echo -e "${RED}启动失败。${NC}"; fi
}

# --- 11-13. 辅助功能 ---
show_logs() {
    local target_name="$1"
    if [ -z "$target_name" ]; then echo "用法: ruma logs [容器名]"; show_ps; exit 1; fi
    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"
    echo -e "${BLUE}显示日志...${NC}"
    journalctl -u "$svc" -f
}

edit_config() {
    local target_name="$1"
    if [ -z "$target_name" ]; then echo "用法: ruma edit [容器名]"; show_ps; exit 1; fi
    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"
    local svc_file="/etc/systemd/system/${svc}.service"
    nano "$svc_file"
    systemctl daemon-reload
    systemctl restart "$svc"
}

# --- 14. 进入容器 (Exec) ---
enter_container() {
    local target_name="$1"
    local cmd="${2:-/bin/sh}"
    if [ -z "$target_name" ]; then echo "用法: ruma exec [容器名] [命令]"; show_ps; exit 1; fi
    local record=$(grep "^CONTAINER|$target_name|" "$CONFIG_FILE")
    if [ -z "$record" ]; then echo -e "${RED}错误: 找不到容器。${NC}"; return; fi
    IFS='|' read -r _ name img path svc <<< "$record"
    echo -e "${BLUE}进入容器: $name ($path)${NC}"
    "$RURIMA_BIN" r -u "$path" "$cmd"
}

# --- 15. 导入容器 (Import) ---
import_container() {
    local tar_file="$1"
    local name="$2"
    local cmd="$3"
    local auto_start="$4"
    local mount_args="$5"
    local env_args="$6"
    local work_dir="$7"

    if [ -z "$tar_file" ] || [ -z "$name" ]; then echo "参数错误"; exit 1; fi
    local install_path="$DEFAULT_CONTAINER_ROOT/$name"
    local service_name="$name"
    local service_file="/etc/systemd/system/${service_name}.service"

    echo -e "${BLUE}正在导入容器: $name${NC}"
    mkdir -p "$install_path"
    
    echo -e "${BLUE}解压文件...${NC}"
    tar -xpf "$tar_file" --numeric-owner -C "$install_path"

    # 处理 docker save 格式 (分层镜像)
    if [ -f "$install_path/manifest.json" ]; then
        echo -e "${YELLOW}检测到 Docker 镜像格式 (docker save)，正在合并层...${NC}"
        # 获取层列表
        local layers=$(jq -r '.[0].Layers[]' "$install_path/manifest.json")
        for layer in $layers; do
            echo "提取层: $layer"
            tar -xpf "$install_path/$layer" -C "$install_path"
        done
        
        # 清理元数据和层文件
        echo "清理临时文件..."
        rm -f "$install_path"/*.json "$install_path"/repositories
        # 删除层目录
        for layer in $layers; do rm -rf "$install_path/$(dirname "$layer")"; done
    fi
    
    # DNS 配置
    local dns_file="/root/resolv.conf"
    if [ ! -f "$dns_file" ]; then echo -e "nameserver 223.5.5.5\nnameserver 8.8.8.8" > "$dns_file"; fi
    local auto_dns="-M $dns_file /etc/resolv.conf"
    if [[ "$mount_args" == *"/etc/resolv.conf"* ]]; then auto_dns=""; fi

    local workdir_flag=""
    if [ -n "$work_dir" ]; then workdir_flag="-W $work_dir"; fi

    local final_exec="$RURIMA_BIN r -u $workdir_flag $auto_dns $mount_args $env_args $install_path $cmd"
    final_exec=$(echo "$final_exec" | tr -s ' ')
    
    echo -e "启动命令: $final_exec"

    cat > "$service_file" <<EOF
[Unit]
Description=Rurima Container - $service_name
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/root
ExecStart=$final_exec
TimeoutStopSec=30
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    if [[ "$auto_start" =~ ^[yY] ]]; then systemctl enable "$service_name" >/dev/null 2>&1; fi
    
    systemctl start "$service_name"
    sleep 2
    if systemctl is-active --quiet "$service_name"; then
        save_record "$name" "imported" "$install_path" "$service_name"
        echo -e "${GREEN}导入成功!${NC}"
    else
        echo -e "${RED}启动失败。${NC}"
    fi
}

show_help() {
    echo -e "Ruma v${RUMA_VERSION}"
    echo "  run, ps, rm, start, stop, restart, backup, restore, update, logs, edit, exec, import"
}

show_version() {
    init_config "check_only"
    echo "Ruma v${RUMA_VERSION} (Mirror: ${DOCKER_MIRROR})"
}

# --- 主入口 ---
check_dependencies
if [[ "$1" == "-h" || "$1" == "--help" ]]; then show_help; exit 0; fi
if [[ "$1" == "-v" || "$1" == "--version" ]]; then show_version; exit 0; fi

init_config

case "$1" in
    "")
        echo "------------------------------------------------"
        read -p "镜像名 (如 linuxserver/heimdall): " IMAGE_NAME
        if [ -z "$IMAGE_NAME" ]; then echo "不能为空"; exit 1; fi
        
        read -p "容器名称 (可选，默认自动生成): " INPUT_NAME
        if [ -n "$INPUT_NAME" ]; then
            IMG_SHORT_NAME="$INPUT_NAME"
        else
            TMP_NAME="${IMAGE_NAME##*/}"; IMG_SHORT_NAME="${TMP_NAME%%:*}"
        fi
        
        read -p "安装目录 (默认 $IMG_SHORT_NAME): " INPUT_DIR
        if [ -z "$INPUT_DIR" ]; then FINAL_INSTALL_DIR="$DEFAULT_CONTAINER_ROOT/$IMG_SHORT_NAME"
        elif [[ "$INPUT_DIR" == /* ]]; then FINAL_INSTALL_DIR="$INPUT_DIR"
        else FINAL_INSTALL_DIR="$DEFAULT_CONTAINER_ROOT/$INPUT_DIR"; fi
        
        read -p "开机自启 [y/N]: " INPUT_AUTO
        AUTO_START=${INPUT_AUTO:-n}
        
        echo "挂载 (/host:/cont) > "
        read INPUT_MOUNTS
        echo "变量 (K=V) > "
        read INPUT_ENVS
        deploy_container "$IMAGE_NAME" "$FINAL_INSTALL_DIR" "$(parse_mounts "$INPUT_MOUNTS")" "$(parse_envs "$INPUT_ENVS")" "$AUTO_START" "-u" "" "" "$INPUT_NAME"
        ;;

    run)
        shift
        CLI_MOUNT_STR=""; CLI_ENV_STR=""
        CLI_UNSHARE="true"; CLI_AUTORUN="n"; CLI_IMAGE=""; CLI_NAME=""
        while [[ $# -gt 0 ]]; do
            case $1 in
                -u) CLI_UNSHARE="true"; shift ;;
                -v) val="$2"; if [[ "$val" == *":"* ]]; then src="${val%%:*}"; tgt="${val#*:}"; src=$(get_abs_path "$src"); check_and_create_mount_src "$src"; CLI_MOUNT_STR="$CLI_MOUNT_STR -m $src $tgt"; else echo -e "${RED}Error: -v${NC}"; exit 1; fi; shift 2 ;;
                -e) val="$2"; if [[ "$val" == *"="* ]]; then k="${val%%=*}"; v="${val#*=}"; CLI_ENV_STR="$CLI_ENV_STR -e $k $v"; else echo -e "${RED}Error: -e${NC}"; exit 1; fi; shift 2 ;;
                --name) CLI_NAME="$2"; shift 2 ;;
                --autorun) CLI_AUTORUN="$2"; shift 2 ;;
                *) CLI_IMAGE="$1"; shift ;;
            esac
        done
        if [ -z "$CLI_IMAGE" ]; then echo -e "${RED}Error: 未指定镜像名。${NC}"; exit 1; fi
        
        if [ -n "$CLI_NAME" ]; then
            img_short_name="$CLI_NAME"
        else
            tmp_name="${CLI_IMAGE##*/}"; img_short_name="${tmp_name%%:*}"
        fi
        install_dir="$DEFAULT_CONTAINER_ROOT/$img_short_name"
        
        deploy_container "$CLI_IMAGE" "$install_dir" "" "" "$CLI_AUTORUN" "$CLI_UNSHARE" "$CLI_MOUNT_STR" "$CLI_ENV_STR" "$CLI_NAME"
        ;;
    
    ps) show_ps ;;
    rm) remove_container "$2" "$3" ;;
    start) manage_service "start" "$2" ;;
    stop) manage_service "stop" "$2" ;;
    restart) manage_service "restart" "$2" ;;
    backup) backup_container "$2" ;;
    restore) restore_container "$2" "$3" ;;
    logs) show_logs "$2" ;;
    update) update_container "$2" "$3" ;;
    edit) edit_config "$2" ;;
    exec) enter_container "$2" "$3" ;;
    import)
        shift
        tar_file="" container_name="" start_cmd="" auto_start="n"
        CLI_MOUNT_STR=""; CLI_ENV_STR=""; CLI_WORKDIR=""
        while [[ $# -gt 0 ]]; do
            case $1 in
                -f) tar_file="$2"; shift 2 ;;
                -n) container_name="$2"; shift 2 ;;
                -c) start_cmd="$2"; shift 2 ;;
                -W) CLI_WORKDIR="$2"; shift 2 ;;
                --autorun) auto_start="$2"; shift 2 ;;
                -v) val="$2"; if [[ "$val" == *":"* ]]; then src="${val%%:*}"; tgt="${val#*:}"; src=$(get_abs_path "$src"); check_and_create_mount_src "$src"; CLI_MOUNT_STR="$CLI_MOUNT_STR -m $src $tgt"; fi; shift 2 ;;
                -e) val="$2"; if [[ "$val" == *"="* ]]; then k="${val%%=*}"; v="${val#*=}"; CLI_ENV_STR="$CLI_ENV_STR -e $k $v"; fi; shift 2 ;;
                *) shift ;;
            esac
        done
        import_container "$tar_file" "$container_name" "$start_cmd" "$auto_start" "$CLI_MOUNT_STR" "$CLI_ENV_STR" "$CLI_WORKDIR"
        ;;
    *) echo "Error: Unknown command"; exit 1 ;;
esac