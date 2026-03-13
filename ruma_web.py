#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import subprocess
import json
import time
import threading
import uuid
import re
import tarfile
import shlex
import hmac
import atexit
import logging
import glob
import secrets
import psutil
from flask import Flask, jsonify, request, render_template_string

# === 配置日志 ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.expanduser('~/ruma_web.log')),
        logging.StreamHandler()
    ]
)

# === 配置 ===
PORT = 5777
RUMA_BIN = "/usr/local/bin/ruma"
CONFIG_FILE = os.path.expanduser("~/.ruma_config")

# 备份策略配置
BACKUP_SCHEDULE_FILE = os.path.expanduser("~/.ruma_backup_schedule")

def load_backup_schedule():
    """加载备份调度配置"""
    schedule = {}
    if os.path.exists(BACKUP_SCHEDULE_FILE):
        try:
            with open(BACKUP_SCHEDULE_FILE, 'r') as f:
                schedule = json.load(f)
        except:
            pass
    return schedule

def save_backup_schedule(schedule):
    """保存备份调度配置"""
    with open(BACKUP_SCHEDULE_FILE, 'w') as f:
        json.dump(schedule, f)

def cleanup_old_backups(container_name, keep_days):
    """清理过期备份"""
    backup_dir = os.path.expanduser("~/ruma_backups")
    if not os.path.exists(backup_dir):
        return
    
    import datetime
    cutoff = datetime.datetime.now() - datetime.timedelta(days=keep_days)
    
    for f in os.listdir(backup_dir):
        if f.startswith(container_name + "_"):
            fpath = os.path.join(backup_dir, f)
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
                if mtime < cutoff:
                    os.remove(fpath)
            except:
                pass

def run_backup(container_name, auto=False):
    """执行备份"""
    path, svc = get_container_info(container_name)
    if not path:
        return False, "Container not found"
    
    backup_dir = os.path.expanduser("~/ruma_backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(backup_dir, f"{container_name}_{timestamp}.tar.gz")
    
    # 停止服务
    subprocess.run(["systemctl", "stop", svc], capture_output=True)
    time.sleep(1)
    
    try:
        # 打包
        with tarfile.open(backup_file, "w:gz") as tar:
            tar.add(path, arcname=os.path.basename(path))
        
        return True, backup_file
    except Exception as e:
        return False, str(e)
    finally:
        # 重启服务
        subprocess.run(["systemctl", "start", svc], capture_output=True)

# 定时备份线程
backup_thread = None
backup_thread_running = False

def backup_scheduler_loop():
    """定时备份调度循环"""
    global backup_thread_running
    while backup_thread_running:
        try:
            schedule = load_backup_schedule()
            now = datetime.datetime.now()
            
            for container_name, config in schedule.items():
                if not config.get('enabled', False):
                    continue
                
                interval = config.get('interval', 1)  # 天
                keep_days = config.get('keep_days', 7)
                last_backup = config.get('last_backup', '')
                
                # 检查是否需要备份
                should_backup = False
                if not last_backup:
                    should_backup = True
                else:
                    try:
                        last_time = datetime.datetime.strptime(last_backup, "%Y%m%d_%H%M%S")
                        if (now - last_time).days >= interval:
                            should_backup = True
                    except:
                        should_backup = True
                
                if should_backup:
                    success, result = run_backup(container_name, auto=True)
                    if success:
                        config['last_backup'] = time.strftime("%Y%m%d_%H%M%S")
                        config['last_result'] = 'ok'
                    else:
                        config['last_result'] = result
                    
                    # 清理旧备份
                    cleanup_old_backups(container_name, keep_days)
            
            save_backup_schedule(schedule)
            
        except Exception as e:
            logging.error(f"Backup scheduler error: {e}")
        
        # 每小时检查一次
        time.sleep(3600)

def start_backup_scheduler():
    """启动定时备份调度器"""
    global backup_thread, backup_thread_running
    if backup_thread is None or not backup_thread.is_alive():
        backup_thread_running = True
        backup_thread = threading.Thread(target=backup_scheduler_loop, daemon=True)
        backup_thread.start()

# 启动时检查并启动调度器
start_backup_scheduler()

import datetime
BACKUP_DIR = os.path.expanduser("~/ruma_backups")
TEMPLATE_DIR = os.path.expanduser("~/ruma-compose")
MAX_FILE_SIZE = 1024 * 1024  # 1MB
MAX_TASKS = 1000  # 最大任务数
MAX_TASK_AGE_HOURS = 24  # 任务保留最大小时数
MAX_TASK_LOGS = 1000  # 每个任务最大日志条数
if not os.path.exists(TEMPLATE_DIR): os.makedirs(TEMPLATE_DIR)
if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
API_KEY = ""

# === 线程锁 ===
tasks_lock = threading.Lock()

# === 安全辅助函数 ===

def safe_path_join(base_path, user_path):
    """安全地拼接路径并验证结果在base_path内"""
    base = os.path.abspath(base_path)
    rel_path = user_path.lstrip('/')
    target = os.path.abspath(os.path.join(base, rel_path))
    if not target.startswith(base + os.sep) and target != base:
        return None
    return target

# 预编译正则表达式
ALLOW_CHARS_RE = re.compile(r'^[a-zA-Z0-9_\-\./:]+$')

def validate_command_arg(arg):
    """验证命令参数不包含危险字符"""
    return bool(ALLOW_CHARS_RE.match(str(arg)))

def constant_time_compare(val1, val2):
    """恒定时间比较，防止时序攻击"""
    return hmac.compare_digest(str(val1), str(val2))

def safe_filename(filename):
    """验证文件名不包含路径遍历字符"""
    if filename is None:
        return None
    if '..' in filename or '/' in filename or '\\' in filename:
        return None
    # 移除控制字符
    return re.sub(r'[\x00-\x1f\x7f]', '', filename)

def validate_container_name(name):
    """验证容器名称只包含安全字符"""
    if not name:
        return False
    return bool(re.match(r'^[a-zA-Z0-9_\-]+$', name))

# === 临时文件清理 ===
def cleanup_temp_files():
    """退出时清理临时文件"""
    temp_patterns = [
        os.path.join(BACKUP_DIR, "import_*_*.tar.gz"),
        os.path.join(BACKUP_DIR, "upload_temp_*.tar.gz")
    ]
    for pattern in temp_patterns:
        for f in glob.glob(pattern):
            try:
                if os.path.getmtime(f) < time.time() - 3600:
                    os.remove(f)
            except (OSError, IOError):
                pass

atexit.register(cleanup_temp_files)

def load_api_key():
    global API_KEY
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            m = re.search(r'API_KEY="(.*?)"', f.read())
            API_KEY = m.group(1) if m else ""
    if not API_KEY or len(API_KEY) < 16:
        API_KEY = secrets.token_hex(32)
        with open(CONFIG_FILE, 'a' if os.path.exists(CONFIG_FILE) else 'w') as f:
            f.write(f'\nAPI_KEY="{API_KEY}"\n')
    logging.info(f"--- API KEY: {API_KEY[:8]}... ---")

app = Flask(__name__)

TASKS = {}

def run_background_task(task_id, cmd_list, meta=None):
    with tasks_lock:
        TASKS[task_id] = {'status': 'running', 'logs': [], 'result': '', 'meta': meta or {}, 'created_at': time.time()}

    def log(msg):
        with tasks_lock:
            TASKS[task_id]['logs'].append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    process = None
    try:
        log(f"执行: {' '.join(cmd_list)}")
        process = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
            start_new_session=True
        )

        with process.stdout as stdout:
            for line in stdout:
                log(line.strip())

        process.wait()
        with tasks_lock:
            TASKS[task_id]['status'] = 'done' if process.returncode == 0 else 'error'
    except Exception as e:
        with tasks_lock:
            TASKS[task_id]['status'] = 'error'
        error_msg = f"Error: {str(e)}"
        log(error_msg)
        logging.error(f"Task {task_id} failed: {error_msg}", exc_info=True)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

# === 任务清理线程 ===
def cleanup_old_tasks():
    """定期清理过期任务"""
    while True:
        try:
            with tasks_lock:
                current_time = time.time()
                tasks_to_remove = []
                for tid, task in TASKS.items():
                    # 限制日志大小
                    if 'logs' in task and len(task['logs']) > MAX_TASK_LOGS:
                        task['logs'] = task['logs'][-MAX_TASK_LOGS:]
                    # 清理过期任务
                    if task['status'] in ('done', 'error'):
                        created_at = task.get('created_at', 0)
                        if current_time - created_at > MAX_TASK_AGE_HOURS * 3600:
                            tasks_to_remove.append(tid)
                if len(TASKS) > MAX_TASKS:
                    sorted_tasks = sorted(
                        [(tid, TASKS[tid].get('created_at', 0)) for tid in TASKS],
                        key=lambda x: x[1]
                    )
                    tasks_to_remove.extend([tid for tid, _ in sorted_tasks[:len(TASKS) - MAX_TASKS]])
                for tid in tasks_to_remove:
                    del TASKS[tid]
        except Exception as e:
            logging.error(f"Task cleanup error: {e}")
        time.sleep(300)

def start_task_cleanup_thread():
    t = threading.Thread(target=cleanup_old_tasks, daemon=True)
    t.start()

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ruma 容器管理</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css">
    <style>
        /* SaltUI 风格重构 */
        :root {
            --bg-primary: #0d0d0d;
            --bg-secondary: #1a1a1a;
            --bg-card: #161616;
            --bg-card-hover: #1e1e1e;
            --accent: #5e7ce2;
            --accent-gradient: linear-gradient(135deg, #5e7ce2 0%, #7c4dff 100%);
            --accent-glow: rgba(94, 124, 226, 0.3);
            --text-primary: #ffffff;
            --text-secondary: #8e8e93;
            --border-color: #2c2c2e;
            --success: #34c759;
            --danger: #ff453a;
            --warning: #ffd60a;
        }
        
        * { box-sizing: border-box; }
        
        body {
            background: var(--bg-primary);
            background-image: 
                radial-gradient(ellipse at top, rgba(94, 124, 226, 0.15) 0%, transparent 50%),
                radial-gradient(ellipse at bottom right, rgba(124, 77, 255, 0.1) 0%, transparent 50%);
            min-height: 100vh;
            color: var(--text-primary);
        }
        
        .navbar {
            background: rgba(22, 22, 22, 0.8) !important;
            backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border-color);
            padding: 0.75rem 0;
        }
        
        .navbar-brand {
            background: var(--accent-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 700;
            font-size: 1.5rem;
            letter-spacing: -0.5px;
        }
        
        .card {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            overflow: hidden;
            transition: all 0.3s ease;
        }
        
        .card:hover {
            border-color: var(--accent);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3), 0 0 0 1px var(--accent-glow);
            transform: translateY(-2px);
        }
        
        .card-header {
            background: transparent;
            border-bottom: 1px solid var(--border-color);
            padding: 1rem 1.25rem;
        }
        
        .card-body { padding: 1.25rem; }
        
        .btn {
            border-radius: 12px;
            padding: 0.5rem 1.25rem;
            font-weight: 500;
            transition: all 0.2s ease;
        }
        
        .btn:active { transform: scale(0.98); }
        
        .btn-primary {
            background: var(--accent-gradient);
            border: none;
            box-shadow: 0 4px 16px var(--accent-glow);
        }
        
        .btn-primary:hover {
            box-shadow: 0 6px 24px var(--accent-glow);
            transform: translateY(-1px);
        }
        
        .btn-outline-light {
            border-color: var(--border-color);
            color: var(--text-secondary);
        }
        
        .btn-outline-light:hover {
            background: var(--bg-card-hover);
            border-color: var(--accent);
            color: var(--text-primary);
        }
        
        .btn-success { background: var(--success); border: none; }
        .btn-danger { background: var(--danger); border: none; }
        .btn-warning { background: var(--warning); border: none; color: #000; }
        
        .table {
            --bs-table-bg: transparent;
            --bs-table-color: var(--text-primary);
            --bs-table-border-color: var(--border-color);
        }
        
        .table-dark { --bs-table-bg: transparent; }
        
        .table > :not(caption) > * > * {
            padding: 1rem;
            border-bottom-color: var(--border-color);
        }
        
        .table > thead {
            background: rgba(94, 124, 226, 0.1);
        }
        
        .table > tbody > tr:hover {
            background: var(--bg-card-hover);
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
            margin-right: 8px;
            animation: pulse 2s infinite;
        }
        
        .dot-green { 
            background: var(--success); 
            box-shadow: 0 0 8px var(--success);
        }
        .dot-red { 
            background: var(--danger);
            box-shadow: 0 0 8px var(--danger);
            animation: none;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        pre.log-box, .console-out { 
            background: #0a0a0a; 
            color: #00ff88; 
            padding: 16px; 
            border-radius: 12px; 
            font-family: 'SF Mono', 'Fira Code', monospace;
            font-size: 13px;
            border: 1px solid var(--border-color);
        }
        
        .config-group { 
            background: rgba(255,255,255,0.03); 
            padding: 20px; 
            border-radius: 16px; 
            margin-bottom: 20px; 
            border: 1px solid var(--border-color);
        }
        
        .config-label { 
            font-weight: 600; 
            color: var(--text-primary); 
            margin-bottom: 16px; 
            display: block;
            font-size: 0.9rem;
        }
        
        .form-control, .form-select { 
            background: var(--bg-primary); 
            border: 1px solid var(--border-color); 
            color: var(--text-primary);
            border-radius: 12px;
            padding: 0.75rem 1rem;
        }
        
        .form-control:focus, .form-select:focus { 
            background: var(--bg-primary); 
            color: var(--text-primary); 
            border-color: var(--accent);
            box-shadow: 0 0 0 3px var(--accent-glow);
        }
        
        .form-control::placeholder { color: var(--text-secondary); }
        
        .input-group-text { 
            background: var(--bg-card); 
            border-color: var(--border-color); 
            color: var(--text-secondary);
            border-radius: 12px 0 0 12px;
        }
        
        .nav-tabs {
            border-bottom-color: var(--border-color);
            gap: 8px;
        }
        
        .nav-tabs .nav-link {
            border: none;
            color: var(--text-secondary);
            padding: 12px 24px;
            border-radius: 12px;
            transition: all 0.2s;
        }
        
        .nav-tabs .nav-link:hover {
            color: var(--text-primary);
            background: var(--bg-card-hover);
        }
        
        .nav-tabs .nav-link.active {
            background: var(--accent-gradient);
            color: white;
        }
        
        .badge {
            padding: 6px 12px;
            border-radius: 8px;
            font-weight: 500;
        }
        
        .modal-content {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 20px;
        }
        
        .modal-header { border-bottom-color: var(--border-color); }
        .modal-footer { border-top-color: var(--border-color); }
        
        /* 资源监控样式 */
        .resource-card {
            background: linear-gradient(135deg, rgba(94, 124, 226, 0.1) 0%, rgba(124, 77, 255, 0.05) 100%);
            border-radius: 12px;
            padding: 12px;
            margin: 4px 0;
        }
        
        .resource-item {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85rem;
        }
        
        .resource-icon {
            width: 28px;
            height: 28px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
        }
        
        .resource-cpu { background: rgba(94, 124, 226, 0.2); color: #5e7ce2; }
        .resource-mem { background: rgba(52, 199, 89, 0.2); color: #34c759; }
        
        /* 动画 */
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .page-section {
            animation: fadeIn 0.3s ease;
        }
        
        /* 响应式 */
        @media (max-width: 991.98px) {
            .navbar-collapse { 
                background: var(--bg-card); 
                padding: 1rem; 
                border-radius: 16px; 
                margin-top: 1rem; 
                border: 1px solid var(--border-color); 
            }
        }
        
        @media (max-width: 767.98px) {
            /* 移动端适配 */
            body { padding-bottom: 0; }
            
            .navbar { padding: 0.5rem 0; }
                min-width: 50px;
            }
            
            .navbar { padding: 0.5rem 0; }
            .navbar-brand { font-size: 1.25rem; }
            
            .card { border-radius: 12px; }
            .card-header { padding: 0.75rem 1rem; }
            .card-body { padding: 1rem; }
            
            /* 移动端按钮 - 更大更易点击 */
            .btn { 
                padding: 0.75rem 1rem; 
                font-size: 0.9rem;
                min-height: 44px;
                min-width: 44px;
            }
            
            .btn-sm {
                padding: 0.5rem 0.75rem;
                min-height: 36px;
                min-width: 36px;
            }
            
            /* 表格横向滚动 */
            .table-responsive { 
                -webkit-overflow-scrolling: touch;
                margin: 0 -1rem;
                padding: 0 1rem;
            }
            
            .table > :not(caption) > * > * {
                padding: 0.75rem 0.5rem;
                font-size: 0.85rem;
            }
            
            /* 资源监控卡片更紧凑 */
            .resource-card { padding: 8px; }
            .resource-item { font-size: 0.75rem; }
            .resource-icon { width: 24px; height: 24px; font-size: 12px; }
            
            /* 表单 */
            .form-control, .form-select {
                padding: 0.625rem 0.875rem;
                font-size: 0.9rem;
            }
            
            /* 模态框全屏 */
            .modal-xl, .modal-lg, .modal-dialog { 
                margin: 0; 
                max-width: 100%; 
                height: 100%;
            }
            .modal-content { 
                border-radius: 0; 
                height: 100%;
                border: none;
            }
            .modal-body { overflow-y: auto; }
            
            /* 标签页横向滚动 */
            .nav-tabs { 
                overflow-x: auto;
                white-space: nowrap;
                -webkit-overflow-scrolling: touch;
                padding-bottom: 5px;
            }
            .nav-tabs .nav-link {
                padding: 0.5rem 1rem;
                font-size: 0.85rem;
            }
            
            /* 输入组更紧凑 */
            .input-group-text {
                padding: 0.5rem 0.75rem;
                font-size: 0.85rem;
            }
            
            /* 隐藏非必要元素 */
            .hide-mobile { display: none !important; }
            
            /* 标签页容器 */
            .tab-content { min-height: 200px; }
            
            /* 配置组 */
            .config-group { 
                padding: 12px; 
                border-radius: 12px; 
                margin-bottom: 12px; 
            }
            .config-label { margin-bottom: 10px; font-size: 0.85rem; }
            
            /* 日志框 */
            pre.log-box, .console-out { 
                border-radius: 8px; 
                font-size: 12px;
                padding: 12px;
            }
            
            /* 底部导航栏 */
            .mobile-nav {
                display: flex;
                position: fixed;
                bottom: 20px;
                left: 50%;
                transform: translateX(-50%);
                background: rgba(22, 22, 22, 0.9);
                backdrop-filter: blur(20px);
                border: 1px solid var(--border-color);
                padding: 0.5rem 1.5rem;
                z-index: 1000;
                justify-content: center;
                border-radius: 20px;
                box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
            }
            
            .mobile-nav-btn {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 0.5rem 1rem;
                color: var(--text-secondary);
                text-decoration: none;
                font-size: 0.75rem;
                min-width: 60px;
                border-radius: 12px;
                transition: all 0.2s ease;
                margin: 0 4px;
            }
            
            .mobile-nav-btn:hover {
                background: var(--bg-card-hover);
                color: var(--text-primary);
            }
            
            .mobile-nav-btn.active {
                color: var(--accent);
                background: rgba(94, 124, 226, 0.15);
            }
            
            .mobile-nav-btn i {
                font-size: 1.25rem;
                margin-bottom: 2px;
            }
            
            /* 页面标题 */
            h5, .h5 { font-size: 1rem; }
            
            /* 间距调整 */
            .mb-4 { margin-bottom: 1rem !important; }
            .g-3 { gap: 0.75rem !important; }
            .p-4 { padding: 1rem !important; }
            
            /* 表格操作按钮 */
            .table .btn { padding: 0.25rem 0.5rem; }
        }
        
        /* 隐藏桌面端导航 */
        @media (min-width: 768px) {
            .mobile-nav { display: none !important; }
        }
        
        /* 滚动条 */
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: var(--bg-primary); }
        ::-webkit-scrollbar-thumb { background: var(--border-color); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--accent); }
    </style>
</head>
<body>

<nav class="navbar navbar-expand-lg navbar-dark mb-4 sticky-top">
  <div class="container-fluid px-4">
    <a class="navbar-brand" href="#">
        <i class="bi bi-box-fill me-2"></i>Ruma
    </a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav" style="border: none;">
      <i class="bi bi-list" style="font-size: 1.5rem;"></i>
    </button>
    <div class="collapse navbar-collapse" id="navbarNav">
        <div class="d-flex gap-3 ms-auto navbar-nav-btns align-items-center">
            <button class="btn btn-outline-light btn-sm" onclick="showTasksPage()">
                <i class="bi bi-list-task me-1"></i> 任务
            </button>
            <button class="btn btn-outline-light btn-sm" onclick="showSettings()">
                <i class="bi bi-gear me-1"></i> 设置
            </button>
            <button class="btn btn-primary btn-sm" onclick="showDeployPage()">
                <i class="bi bi-plus-lg me-1"></i> 部署
            </button>
        </div>
    </div>
  </div>
</nav>

<div id="main-page" class="container-fluid page-section px-4">
    <div class="row">
        <div class="col-12">
            <div class="card">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <h5 class="mb-0 fw-bold">
                        <i class="bi bi-grid-3x3-gap me-2 text-primary"></i>容器列表
                    </h5>
                    <button class="btn btn-outline-light btn-sm" onclick="loadContainers()">
                        <i class="bi bi-arrow-clockwise"></i>
                    </button>
                </div>
                <div class="card-body p-0">
                    <div class="table-responsive">
                        <table class="table table-dark table-hover mb-0 align-middle">
                            <thead>
                                <tr>
                                    <th>状态</th>
                                    <th>名称</th>
                                    <th>端口</th>
                                    <th>资源</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody id="container-table-body">
                                <tr><td colspan="5" class="text-center py-5"><div class="spinner-border text-primary" role="status"></div><div class="mt-2 text-muted">加载中...</div></td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<div id="tasks-page" class="container page-section" style="display:none;">
    <div class="card shadow-sm">
        <div class="card-header d-flex justify-content-between align-items-center">
            <h5 class="mb-0">任务列表</h5>
            <button class="btn btn-sm btn-outline-secondary" onclick="showMainPage()">返回首页</button>
        </div>
        <div class="card-body p-0">
            <div class="table-responsive">
                <table class="table table-dark table-hover mb-0 align-middle">
                    <thead><tr><th>时间</th><th>类型</th><th>名称/详情</th><th>状态</th><th>操作</th></tr></thead>
                    <tbody id="task-list-body"></tbody>
                </table>
            </div>
        </div>
    </div>
</div>

<div id="task-view-page" class="container page-section" style="display:none;">
    <div class="card shadow-sm">
        <div class="card-header d-flex justify-content-between align-items-center">
            <h5 class="mb-0">任务日志</h5>
            <button class="btn btn-sm btn-outline-secondary" onclick="showTasksPage()">返回列表</button>
        </div>
        <div class="card-body">
             <div class="d-flex justify-content-between align-items-center mb-2">
                <div>
                    <span id="task-view-name" class="fw-bold me-2"></span>
                    <span id="task-view-status" class="badge bg-secondary">Unknown</span>
                </div>
                <div class="spinner-border spinner-border-sm text-primary" role="status" id="task-view-spinner" style="display:none;"></div>
            </div>
            <pre id="task-view-log" class="log-box" style="height: 500px;"></pre>
        </div>
    </div>
</div>

<div id="deploy-page" class="container page-section" style="display:none;">
    <div class="card shadow-sm">
        <div class="card-header d-flex justify-content-between align-items-center flex-wrap gap-2">
            <h5 class="mb-0">部署新容器</h5>
            <div><button class="btn btn-sm btn-outline-info me-2" onclick="showTemplateModal()">使用模板</button><button class="btn btn-sm btn-outline-secondary" onclick="showMainPage()">返回列表</button></div>
        </div>
        <div class="card-body">
            <form id="installForm">
                <ul class="nav nav-tabs mb-3" role="tablist">
                    <li class="nav-item"><button class="nav-link active" data-bs-target="#install-mode-pull" data-bs-toggle="tab" type="button">拉取镜像</button></li>
                    <li class="nav-item"><button class="nav-link" data-bs-target="#install-mode-import" data-bs-toggle="tab" type="button">本地导入</button></li>
                </ul>
                <div class="tab-content">
                    <div class="tab-pane fade show active" id="install-mode-pull">
                        <div class="config-group"><span class="config-label">镜像设置</span><div class="row g-3">
                            <div class="col-md-12 mb-2"><label class="small text-secondary">镜像名 (如 linuxserver/heimdall)</label><div class="input-group"><span class="input-group-text" id="install-mirror-prefix">...</span><input type="text" class="form-control" id="install-image"></div></div>
                            <div class="col-md-12 mb-2"><label class="small text-secondary">容器名称 (可选，默认为镜像名)</label><input type="text" class="form-control" id="install-name-pull" placeholder="自定义名称，留空则自动生成"></div>
                            <div class="col-md-12 d-flex justify-content-between align-items-center"><label class="small text-secondary">临时修改镜像源</label><div class="form-check form-switch"><input class="form-check-input" type="checkbox" id="install-use-mirror" onchange="toggleInstallMirror()"><label class="form-check-label small text-secondary">启用</label></div></div>
                            <div class="col-md-12"><input type="text" class="form-control form-control-sm" id="install-mirror-input" oninput="updateMirrorPrefix()"></div>
                        </div></div>
                    </div>
                    <div class="tab-pane fade" id="install-mode-import">
                        <div class="config-group"><span class="config-label">导入设置</span><div class="row g-3">
                            <div class="col-md-12"><label class="small text-secondary">容器名称</label><input type="text" class="form-control" id="install-name-import"></div>
                            <div class="col-md-12"><label class="small text-secondary">上传 Tar 包</label><div class="input-group"><input type="file" class="form-control" id="install-file"><button class="btn btn-outline-info" type="button" onclick="parseImportFile()">解析</button></div></div>
                            <div class="col-md-6"><label class="small text-secondary">工作目录 (-W)</label><input type="text" class="form-control" id="install-workdir"></div>
                            <div class="col-md-6"><label class="small text-secondary">启动命令</label><input type="text" class="form-control" id="install-cmd" value="/init"></div>
                        </div></div>
                    </div>
                    <input type="hidden" id="install-temp-path">
                </div>
                <div class="config-group mt-3"><div class="form-check form-switch"><input class="form-check-input" type="checkbox" id="install-autorun" checked><label class="form-check-label ms-2">开机自启</label></div></div>
                <div class="config-group"><div class="d-flex justify-content-between align-items-center mb-2"><span class="config-label mb-0">卷映射</span><button type="button" class="btn btn-sm btn-outline-primary" onclick="addMountRow('install')">+ 添加</button></div><div id="install-mount-list"></div></div>
                <div class="config-group"><div class="d-flex justify-content-between align-items-center mb-2"><span class="config-label mb-0">环境变量</span><button type="button" class="btn btn-sm btn-outline-primary" onclick="addEnvRow('install')">+ 添加</button></div><div id="install-env-list"></div></div>
            </form>
            <div class="d-flex justify-content-end mt-3">
                <button type="button" class="btn btn-primary px-4" onclick="startInstall()">开始部署</button>
            </div>
        </div>
    </div>
</div>

<div class="modal fade" id="taskModal" data-bs-backdrop="static" tabindex="-1"><div class="modal-dialog modal-lg"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">执行任务...</h5><button type="button" class="btn-close" onclick="closeTaskModal()"></button></div><div class="modal-body"><div class="progress mb-3"><div id="task-progress" class="progress-bar progress-bar-striped progress-bar-animated" style="width: 100%"></div></div><pre id="task-log" class="log-box"></pre></div></div></div></div>

<div class="modal fade" id="templateModal" tabindex="-1"><div class="modal-dialog modal-xl"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">配置模板仓库</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body">
    <div class="row">
        <div class="col-md-3 border-end">
            <h6 class="text-muted mb-2">已保存模板</h6>
            <div class="list-group" id="template-list" style="height: 400px; overflow-y: auto;"></div>
        </div>
        <div class="col-md-9">
            <div class="input-group mb-2">
                <span class="input-group-text">模板名称</span>
                <input type="text" class="form-control" id="template-name" placeholder="例如: my-app">
                <span class="input-group-text">.yaml</span>
            </div>
            <textarea id="template-input" class="form-control font-monospace mb-3" rows="15" placeholder="-name:my-container&#10;-image:alpine:latest&#10;-mirror:docker.1ms.run&#10;-v /host:/container&#10;-e KEY:VALUE"></textarea>
            <div class="d-flex justify-content-between">
                <div>
                    <button class="btn btn-success me-1" onclick="saveTemplate()">保存模板</button>
                    <button class="btn btn-danger" onclick="deleteTemplate()">删除模板</button>
                </div>
                <button type="button" class="btn btn-primary" onclick="parseTemplate()">解析并部署</button>
            </div>
        </div>
    </div>
</div></div></div></div>

<div class="modal fade" id="loadingModal" data-bs-backdrop="static" data-bs-keyboard="false" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content"><div class="modal-body text-center"><div class="spinner-border text-primary mb-3" role="status"></div><h5 id="loading-msg">处理中...</h5></div></div></div></div>

<div class="modal fade" id="settingsModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">全局配置</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><form id="settingsForm">
    <div class="mb-3"><label>Rurima 路径</label><input type="text" class="form-control" name="bin"></div>
    <div class="mb-3"><label>默认安装目录</label><input type="text" class="form-control" name="root"></div>
    <div class="mb-3 form-check form-switch"><input class="form-check-input" type="checkbox" name="use_mirror" id="set-use-mirror"><label class="form-check-label" for="set-use-mirror">启用镜像加速</label></div>
    <div class="mb-3"><label class="text-warning">Docker 镜像加速源</label><input type="text" class="form-control" name="mirror" placeholder="docker.1ms.run"></div>
</form></div><div class="modal-footer"><button type="button" class="btn btn-primary" onclick="saveSettings()">保存</button></div></div></div></div>

<div class="modal fade" id="editModal" tabindex="-1"><div class="modal-dialog modal-xl"><div class="modal-content"><div class="modal-header"><h5 class="modal-title" id="editModalTitle">管理容器</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body">
    <div class="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2"><ul class="nav nav-tabs mb-0" role="tablist"><li class="nav-item"><button class="nav-link active" data-bs-target="#tab-config" data-bs-toggle="tab">配置</button></li><li class="nav-item"><button class="nav-link" data-bs-target="#tab-files" data-bs-toggle="tab" onclick="loadFiles('/')">文件</button></li><li class="nav-item"><button class="nav-link" data-bs-target="#tab-logs" data-bs-toggle="tab" onclick="loadLogs()">日志</button></li><li class="nav-item"><button class="nav-link" data-bs-target="#tab-console" data-bs-toggle="tab">终端</button></li><li class="nav-item"><button class="nav-link" data-bs-target="#tab-backup" data-bs-toggle="tab" onclick="loadBackups()">备份</button></li></ul><button class="btn btn-outline-info btn-sm" onclick="exportContainerToTemplate()">导出模板</button></div>
    <div class="tab-content">
        <div class="tab-pane fade show active" id="tab-config"><form id="configForm"><input type="hidden" id="conf-old-name"><input type="hidden" id="conf-extra-flags"><div class="config-group"><span class="config-label">基础设置</span><div class="row g-3"><div class="col-md-12"><label class="small text-secondary">容器名称 (修改以重命名)</label><input type="text" class="form-control" id="conf-new-name"></div><div class="col-md-8"><label class="small text-secondary">镜像名</label><input type="text" class="form-control" id="conf-image" disabled readonly></div><div class="col-md-4"><label class="small text-secondary">开机自启</label><div class="form-check form-switch mt-2"><input class="form-check-input" type="checkbox" id="conf-autorun" style="width: 3em; height: 1.5em;"></div></div><div class="col-md-6"><label class="small text-secondary">工作目录 (-W)</label><input type="text" class="form-control" id="conf-workdir" placeholder="/root"></div><div class="col-md-6"><label class="small text-secondary">启动命令</label><input type="text" class="form-control" id="conf-cmd" placeholder="/init"></div></div></div><div class="config-group"><div class="d-flex justify-content-between align-items-center mb-2"><span class="config-label mb-0">环境变量</span><button type="button" class="btn btn-sm btn-outline-primary" onclick="addEnvRow('edit')">+ 添加</button></div><div id="edit-env-list"></div></div><div class="config-group"><div class="d-flex justify-content-between align-items-center mb-2"><span class="config-label mb-0">卷映射</span><button type="button" class="btn btn-sm btn-outline-primary" onclick="addMountRow('edit')">+ 添加</button></div><div id="edit-mount-list"></div></div><div class="d-flex justify-content-end"><button type="button" class="btn btn-success px-4" onclick="saveComplexConfig()">保存并重启</button></div></form></div>
        <div class="tab-pane fade" id="tab-files">
            <div id="file-browser">
                <div class="d-flex gap-2 mb-2 align-items-center"><button class="btn btn-sm btn-secondary" onclick="loadFiles('..')"><i class="bi bi-arrow-up"></i> 上级</button><input type="text" class="form-control form-control-sm font-monospace" id="file-path" readonly value="/"></div>
                <div class="table-responsive" style="max-height: 500px; overflow-y: auto;"><table class="table table-sm table-dark table-hover"><thead><tr><th>名称</th><th>大小</th><th>操作</th></tr></thead><tbody id="file-list"></tbody></table></div>
            </div>
            <div id="file-editor" style="display:none;">
                <div class="d-flex justify-content-between mb-2"><span id="editing-filename" class="fw-bold"></span><div><button class="btn btn-sm btn-success me-2" onclick="saveFile()">保存</button><button class="btn btn-sm btn-secondary" onclick="closeEditor()">关闭</button></div></div>
                <textarea id="file-content" class="form-control font-monospace" style="height: 450px; background: #111; color: #0f0;"></textarea>
            </div>
        </div>
        <div class="tab-pane fade" id="tab-logs">
            <div class="d-flex justify-content-between mb-2 gap-2">
                <input type="text" class="form-control form-control-sm" id="log-search" placeholder="搜索关键词..." onkeyup="if(event.key==='Enter')loadLogs()">
                <select class="form-select form-select-sm" id="log-level" style="width:auto;" onchange="loadLogs()">
                    <option value="">全部</option>
                    <option value="info">INFO</option>
                    <option value="warn">WARN</option>
                    <option value="error">ERROR</option>
                </select>
                <select class="form-select form-select-sm" id="log-lines" style="width:auto;" onchange="loadLogs()">
                    <option value="100">100行</option>
                    <option value="200" selected>200行</option>
                    <option value="500">500行</option>
                    <option value="1000">1000行</option>
                </select>
                <button class="btn btn-sm btn-outline-light" onclick="loadLogs()">刷新</button>
            </div>
            <pre id="container-logs" class="log-box">Loading...</pre>
        </div>
        <div class="tab-pane fade" id="tab-console"><div class="input-group mb-2"><span class="input-group-text">exec</span><input type="text" class="form-control font-monospace" id="console-cmd"><button class="btn btn-primary" onclick="runConsole()">执行</button></div><div id="console-output" class="console-out"></div></div>
        <div class="tab-pane fade" id="tab-backup">
            <!-- 定时备份策略 -->
            <div class="card mb-3 bg-dark border-secondary">
                <div class="card-body p-3">
                    <h6 class="card-title text-light">定时备份策略</h6>
                    <div class="row g-2 align-items-end">
                        <div class="col-auto">
                            <label class="form-label form-label-sm text-muted mb-1">间隔(天)</label>
                            <input type="number" class="form-control form-control-sm" id="backup-interval" value="1" min="1" max="30">
                        </div>
                        <div class="col-auto">
                            <label class="form-label form-label-sm text-muted mb-1">保留(天)</label>
                            <input type="number" class="form-control form-control-sm" id="backup-keep" value="7" min="1" max="90">
                        </div>
                        <div class="col-auto">
                            <div class="form-check form-switch">
                                <input class="form-check-input" type="checkbox" id="backup-enabled">
                                <label class="form-check-label text-light" for="backup-enabled">启用</label>
                            </div>
                        </div>
                        <div class="col-auto">
                            <button class="btn btn-success btn-sm" onclick="saveBackupSchedule()">保存策略</button>
                            <button class="btn btn-outline-danger btn-sm" onclick="deleteBackupSchedule()">删除</button>
                        </div>
                    </div>
                    <div class="mt-2 small">
                        <span class="text-muted">上次备份: </span><span id="last-backup" class="text-light">-</span>
                        <span class="text-muted ms-2">状态: </span><span id="backup-status" class="text-light">-</span>
                    </div>
                </div>
            </div>
            <!-- 旧的 Cron 备份 -->
            <div class="card mb-3 bg-dark border-secondary"><div class="card-body p-3"><h6 class="card-title text-light">定时备份 (Cron)</h6><div class="input-group input-group-sm mb-2"><span class="input-group-text bg-secondary text-light">Cron 表达式</span><input type="text" class="form-control font-monospace" id="cron-expr" placeholder="例如: 0 3 * * *"><button class="btn btn-success" onclick="saveCron()">保存</button><button class="btn btn-outline-danger" onclick="removeCron()">删除</button></div><small class="text-muted">任务将写入 root crontab</small></div></div>
            <div class="d-flex justify-content-between align-items-center mb-2"><h6 class="mb-0">备份文件</h6><button class="btn btn-primary btn-sm" onclick="doBackup()">立即备份</button></div>
            <table class="table table-sm table-dark"><thead><tr><th>文件名</th><th>大小</th><th>操作</th></tr></thead><tbody id="backup-list"></tbody></table>
        </div>
    </div></div></div></div></div>

<div class="modal fade" id="loginModal" data-bs-backdrop="static" data-bs-keyboard="false" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content"><div class="modal-header"><h5 class="modal-title">身份验证</h5></div><div class="modal-body"><p>请输入 API Key (位于 ~/.ruma_config):</p><input type="password" id="login-key" class="form-control" placeholder="API Key"></div><div class="modal-footer"><button type="button" class="btn btn-primary w-100" onclick="doLogin()">登录</button></div></div></div></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
// HTML转义函数，防止XSS
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

const API_STORAGE_KEY = 'ruma_api_key';
const originalFetch = window.fetch;
window.fetch = async (url, options = {}) => {
    if (url.toString().startsWith('/api/')) { options.headers = options.headers || {}; options.headers['X-API-Key'] = localStorage.getItem(API_STORAGE_KEY); }
    const res = await originalFetch(url, options);
    if (res.status === 401 && !url.toString().includes('/api/login')) { new bootstrap.Modal(document.getElementById('loginModal')).show(); }
    return res;
};
async function doLogin() { const k = document.getElementById('login-key').value; const res = await originalFetch('/api/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:k}) }); if(res.ok){ localStorage.setItem(API_STORAGE_KEY, k); bootstrap.Modal.getInstance(document.getElementById('loginModal')).hide(); loadContainers(); } else { alert('密钥无效'); } }

let currentTaskID = null, taskTimer = null, currentContainer = null;

function switchPage(id) { document.querySelectorAll('.page-section').forEach(el => el.style.display = 'none'); document.getElementById(id).style.display = 'block'; }
function showMainPage() { switchPage('main-page'); loadContainers(); }
function showDeployPage() { switchPage('deploy-page'); loadSettingsForDeploy(); }

async function loadContainers() {
    const res = await fetch('/api/containers'); const data = await res.json();
    const tbody = document.getElementById('container-table-body'); tbody.innerHTML = '';
    if(data.length === 0) { tbody.innerHTML = '<tr><td colspan="7" class="text-center">暂无容器</td></tr>'; return; }
    
    // 获取所有容器资源统计
    let statsData = {};
    try {
        const statsRes = await fetch('/api/stats');
        statsData = await statsRes.json();
    } catch(e) { console.error('Failed to load stats:', e); }
    
    function formatBytes(bytes) {
        if(bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }
    
    data.forEach(c => {
        const isRun = c.status === 'RUNNING';
        let portsHtml = '<span class="text-muted small">-</span>';
        if (c.ports && c.ports.length > 0) {
            portsHtml = c.ports.map(p => `<a href="http://${escapeHtml(window.location.hostname)}:${escapeHtml(p)}" target="_blank" class="badge bg-info text-decoration-none me-1">${escapeHtml(p)}</a>`).join('');
        }
        
        // 资源监控
        let statsHtml = '<span class="text-muted small">-</span>';
        if(isRun && statsData[c.name]) {
            const s = statsData[c.name];
            const cpuStr = s.cpu !== undefined ? s.cpu.toFixed(1) + '%' : '-';
            const memStr = s.memory && s.memory.used ? formatBytes(s.memory.used) : '-';
            statsHtml = `<div class="resource-card">
                <div class="resource-item"><div class="resource-icon resource-cpu"><i class="bi bi-cpu"></i></div><span>CPU</span><span class="ms-auto">${cpuStr}</span></div>
                <div class="resource-item mt-2"><div class="resource-icon resource-mem"><i class="bi bi-memory"></i></div><span>MEM</span><span class="ms-auto">${memStr}</span></div>
            </div>`;
        }
        
        tbody.innerHTML += `<tr><td><span class="status-dot ${isRun?'dot-green':'dot-red'}"></span>${escapeHtml(c.status)}</td><td><strong>${escapeHtml(c.name)}</strong></td>
        <td>${portsHtml}</td>
        <td>${statsHtml}</td>
        <td><div class="d-flex gap-1">${isRun?`<button class="btn btn-warning btn-sm" onclick="simpleAction('${escapeHtml(c.name)}','restart')" title="重启"><i class="bi bi-arrow-repeat"></i></button> <button class="btn btn-danger btn-sm" onclick="simpleAction('${escapeHtml(c.name)}','stop')" title="停止"><i class="bi bi-stop-fill"></i></button>`:`<button class="btn btn-success btn-sm" onclick="simpleAction('${escapeHtml(c.name)}','start')" title="启动"><i class="bi bi-play-fill"></i></button>`}<button class="btn btn-primary btn-sm" onclick="openManage('${escapeHtml(c.name)}')" title="管理"><i class="bi bi-gear"></i></button> <button class="btn btn-outline-info btn-sm" onclick="updateContainer('${escapeHtml(c.name)}')" title="更新"><i class="bi bi-arrow-up-circle"></i></button> <button class="btn btn-outline-danger btn-sm" onclick="deleteContainer('${escapeHtml(c.name)}')" title="删除"><i class="bi bi-trash"></i></button></div></td></tr>`;
    });
}

// 任务列表相关
async function showTasksPage() {
    switchPage('tasks-page');
    const res = await fetch('/api/tasks'); const data = await res.json();
    const tbody = document.getElementById('task-list-body'); tbody.innerHTML = '';
    if(data.length === 0) { tbody.innerHTML = '<tr><td colspan="5" class="text-center">暂无任务</td></tr>'; return; }
    data.forEach(t => {
        let badge = 'bg-secondary';
        if(t.status==='running') badge='bg-primary'; else if(t.status==='done') badge='bg-success'; else if(t.status==='error') badge='bg-danger';
        tbody.innerHTML += `<tr><td><small>${escapeHtml(t.time)}</small></td><td>${escapeHtml(t.type)}</td><td>${escapeHtml(t.name)}</td><td><span class="badge ${badge}">${escapeHtml(t.status)}</span></td><td><button class="btn btn-sm btn-outline-light" onclick="viewTask('${escapeHtml(t.id)}')">查看</button></td></tr>`;
    });
}

function viewTask(taskId) {
    currentTaskID = taskId;
    switchPage('task-view-page');
    document.getElementById('task-view-log').innerText = 'Loading...';
    document.getElementById('task-view-spinner').style.display = 'block';
    if(taskTimer) clearInterval(taskTimer);
    
    const poll = async () => {
        const res = await fetch('/api/task/'+taskId); const data = await res.json();
        document.getElementById('task-view-log').innerText = data.logs.join('\n');
        document.getElementById('task-view-status').innerText = data.status;
        document.getElementById('task-view-name').innerText = (data.meta && data.meta.name) ? data.meta.name : 'Task';
        if(data.status !== 'running') {
            clearInterval(taskTimer);
            document.getElementById('task-view-spinner').style.display = 'none';
        }
    };
    poll();
    taskTimer = setInterval(poll, 1000);
}

async function simpleAction(n, a) { if(!confirm(`确认${a}?`))return; const res = await fetch('/api/action', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n, action:a}) }); const data = await res.json(); viewTask(data.task_id); }
async function deleteContainer(n) { if(!confirm('高危操作：删除?'))return; const res = await fetch('/api/action', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n, action:'rm'}) }); const data = await res.json(); viewTask(data.task_id); }
async function updateContainer(n) { if(!confirm(`确认更新容器 ${n}? (将自动备份并拉取最新镜像)`))return; const res = await fetch('/api/update', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n}) }); const data = await res.json(); viewTask(data.task_id); }

function addEnvRow(t, k='', v='') { document.getElementById(t==='edit'?'edit-env-list':'install-env-list').insertAdjacentHTML('beforeend', `<div class="input-group mb-2"><input type="text" class="form-control env-key" value="${k}" placeholder="Key"><input type="text" class="form-control env-val" value="${v}" placeholder="Val"><button class="btn btn-outline-danger btn-remove" onclick="this.parentElement.remove()">X</button></div>`); }
function addMountRow(t, s='', tg='') { document.getElementById(t==='edit'?'edit-mount-list':'install-mount-list').insertAdjacentHTML('beforeend', `<div class="input-group mb-2"><input type="text" class="form-control mount-src" value="${s}" placeholder="宿主"><span class="input-group-text">></span><input type="text" class="form-control mount-tgt" value="${tg}" placeholder="容器"><button class="btn btn-outline-danger btn-remove" onclick="this.parentElement.remove()">X</button></div>`); }

async function openManage(name) {
    currentContainer = name; document.getElementById('editModalTitle').innerText = name; document.getElementById('conf-old-name').value = name; document.getElementById('conf-new-name').value = name;
    const res = await fetch(`/api/config/details/${name}`); const data = await res.json();
    document.getElementById('conf-image').value = data.image; document.getElementById('conf-autorun').checked = data.autorun; document.getElementById('conf-cmd').value = data.cmd || '/init'; document.getElementById('conf-workdir').value = data.workdir || ''; document.getElementById('conf-extra-flags').value = JSON.stringify(data.extra_flags || []);
    document.getElementById('edit-env-list').innerHTML = ''; data.envs.forEach(e => addEnvRow('edit', e.key, e.val));
    document.getElementById('edit-mount-list').innerHTML = ''; data.mounts.forEach(m => addMountRow('edit', m.src, m.tgt));
    new bootstrap.Modal(document.getElementById('editModal')).show();
}
async function saveComplexConfig() {
    const oldName = document.getElementById('conf-old-name').value; const newName = document.getElementById('conf-new-name').value; const cmd = document.getElementById('conf-cmd').value; const workdir = document.getElementById('conf-workdir').value; const extraFlags = JSON.parse(document.getElementById('conf-extra-flags').value || '[]');
    const envs = [...document.querySelectorAll('#edit-env-list .input-group')].map(r => ({key:r.querySelector('.env-key').value, val:r.querySelector('.env-val').value}));
    const mounts = [...document.querySelectorAll('#edit-mount-list .input-group')].map(r => ({src:r.querySelector('.mount-src').value, tgt:r.querySelector('.mount-tgt').value}));
    const res = await fetch('/api/config/save_complex', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name: newName, old_name: oldName, cmd, workdir, extra_flags: extraFlags, autorun:document.getElementById('conf-autorun').checked, envs, mounts}) });
    const data = await res.json(); bootstrap.Modal.getInstance(document.getElementById('editModal')).hide(); viewTask(data.task_id);
}

// 部署逻辑
async function loadSettingsForDeploy() {
    const res = await fetch('/api/settings'); const data = await res.json();
    document.getElementById('install-mirror-input').value = data.mirror;
    document.getElementById('install-use-mirror').checked = data.use_mirror;
    toggleInstallMirror();
    document.getElementById('install-image').value = ''; document.getElementById('install-name-pull').value = ''; document.getElementById('install-temp-path').value = ''; document.getElementById('install-workdir').value = '';
    document.getElementById('install-mount-list').innerHTML = ''; document.getElementById('install-env-list').innerHTML = '';
}
function toggleInstallMirror() { const u = document.getElementById('install-use-mirror').checked; document.getElementById('install-mirror-input').disabled = !u; updateMirrorPrefix(); }
function updateMirrorPrefix() { const u = document.getElementById('install-use-mirror').checked; const v = document.getElementById('install-mirror-input').value; const p = document.getElementById('install-mirror-prefix'); if(u){p.style.display='';p.innerText=v+'/';}else{p.style.display='none';} }
async function startInstall() {
    const isImport = document.getElementById('install-mode-import').classList.contains('active');
    const envs = [...document.querySelectorAll('#install-env-list .input-group')].map(r => ({key:r.querySelector('.env-key').value, val:r.querySelector('.env-val').value}));
    const mounts = [...document.querySelectorAll('#install-mount-list .input-group')].map(r => ({src:r.querySelector('.mount-src').value, tgt:r.querySelector('.mount-tgt').value}));
    const autorun = document.getElementById('install-autorun').checked;
    
    let res, data;
    if (isImport) {
        const name = document.getElementById('install-name-import').value.trim();
        const file = document.getElementById('install-file').files[0];
        const localFile = document.getElementById('install-temp-path').value;
        const cmd = document.getElementById('install-cmd').value.trim();
        const workdir = document.getElementById('install-workdir').value.trim();
        if(!name || (!file && !localFile)) { alert('请填写名称并选择文件'); return; }
        const fd = new FormData(); if(localFile) fd.append('local_file', localFile); else fd.append('file', file);
        fd.append('name', name); fd.append('cmd', cmd); fd.append('workdir', workdir); fd.append('autorun', autorun); fd.append('envs', JSON.stringify(envs)); fd.append('mounts', JSON.stringify(mounts));
        res = await fetch('/api/import', { method:'POST', body:fd });
    } else {
        const imgShort = document.getElementById('install-image').value.trim();
        const name = document.getElementById('install-name-pull').value.trim();
        const mirror = document.getElementById('install-mirror-input').value.trim();
        const use = document.getElementById('install-use-mirror').checked;
        if(!imgShort) { alert('请输入镜像'); return; }
        let finalImg = imgShort; if(use && imgShort.indexOf('.') === -1 && mirror) { finalImg = mirror + '/' + imgShort; }
        res = await fetch('/api/install', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({image:finalImg, name, autorun, envs, mounts}) });
    }
    data = await res.json();
    viewTask(data.task_id);
}

function showTemplateModal() { document.getElementById('template-input').value = ''; document.getElementById('template-name').value = ''; loadTemplates(); new bootstrap.Modal(document.getElementById('templateModal')).show(); }

async function loadTemplates() {
    const res = await fetch('/api/templates'); const list = await res.json();
    const el = document.getElementById('template-list'); el.innerHTML = '';
    list.forEach(f => { const n = f.replace('.yaml',''); el.innerHTML += `<button class="list-group-item list-group-item-action py-2" onclick="loadTemplateContent('${f}')">${n}</button>`; });
}
async function loadTemplateContent(f) {
    const res = await fetch('/api/templates/read', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:f})});
    const d = await res.json(); if(d.content) { document.getElementById('template-input').value = d.content; document.getElementById('template-name').value = f.replace('.yaml',''); }
}
async function saveTemplate() {
    const n = document.getElementById('template-name').value; const c = document.getElementById('template-input').value;
    if(!n || !c) return alert('名称和内容不能为空');
    await fetch('/api/templates', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n, content:c})});
    loadTemplates(); alert('保存成功');
}
async function deleteTemplate() {
    const n = document.getElementById('template-name').value; if(!n || !confirm('确认删除?')) return;
    await fetch('/api/templates/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n+'.yaml'})});
    loadTemplates(); document.getElementById('template-input').value=''; document.getElementById('template-name').value='';
}
async function exportContainerToTemplate() {
    if(!currentContainer) return;
    const res = await fetch(`/api/config/details/${currentContainer}`); const d = await res.json();
    const s = await (await fetch('/api/settings')).json();
    let t = `-name:${currentContainer}\n-image:${d.image}\n` + (s.use_mirror ? `-mirror:${s.mirror}\n` : '');
    if(d.mounts) d.mounts.forEach(m => t+=`-v ${m.src}:${m.tgt}\n`); if(d.envs) d.envs.forEach(e => t+=`-e ${e.key}:${e.val}\n`);
    document.getElementById('template-input').value = t; document.getElementById('template-name').value = currentContainer;
    bootstrap.Modal.getInstance(document.getElementById('editModal')).hide(); new bootstrap.Modal(document.getElementById('templateModal')).show(); loadTemplates();
}

function parseTemplate() {
    const lines = document.getElementById('template-input').value.split('\n');
    document.getElementById('install-mount-list').innerHTML = ''; document.getElementById('install-env-list').innerHTML = '';
    lines.forEach(l => {
        l = l.split('#')[0].trim(); if(!l) return;
        if(l.startsWith('-name:')) document.getElementById('install-name-pull').value = l.substring(6).trim();
        else if(l.startsWith('-image:')) document.getElementById('install-image').value = l.substring(7).trim();
        else if(l.startsWith('-mirror:')) { document.getElementById('install-mirror-input').value = l.substring(8).trim(); document.getElementById('install-use-mirror').checked = true; toggleInstallMirror(); }
        else if(l.startsWith('-v ')) { const p = l.substring(3).trim().split(':'); if(p.length>=2) addMountRow('install', p[0].trim(), p.slice(1).join(':').trim()); }
        else if(l.startsWith('-e ')) { const v = l.substring(3).trim(); let i = v.indexOf(':'); if(i<0) i=v.indexOf('='); if(i>0) addEnvRow('install', v.substring(0,i).trim(), v.substring(i+1).trim()); }
    });
    bootstrap.Modal.getInstance(document.getElementById('templateModal')).hide();
    bootstrap.Tab.getOrCreateInstance(document.querySelector('button[data-bs-target="#install-mode-pull"]')).show();
}

async function parseImportFile() {
    const f = document.getElementById('install-file').files[0];
    if(!f) { alert('请先选择文件'); return; }
    document.getElementById('loading-msg').innerText = "正在上传并解析镜像...";
    const m = new bootstrap.Modal(document.getElementById('loadingModal')); m.show();
    const fd = new FormData(); fd.append('file', f);
    try { const res = await fetch('/api/import/parse', {method:'POST', body:fd}); const d = await res.json();
    if(d.error) alert(d.error); else { 
        document.getElementById('install-cmd').value=d.cmd; 
        document.getElementById('install-workdir').value=d.workdir; 
        document.getElementById('install-temp-path').value=d.path; 
        document.getElementById('install-env-list').innerHTML = '';
        if(d.envs) d.envs.forEach(e => { const p=e.indexOf('='); if(p>0) addEnvRow('install', e.substring(0,p), e.substring(p+1)); });
        alert('解析成功'); 
    }
    } catch(e) { alert('Error: '+e); }
    m.hide();
}

// 其他
async function loadLogs() { 
    if(!currentContainer)return; 
    const search = document.getElementById('log-search').value;
    const level = document.getElementById('log-level').value;
    const lines = document.getElementById('log-lines').value;
    let url = `/api/logs/${currentContainer}?lines=${lines}`;
    if(search) url += `&search=${encodeURIComponent(search)}`;
    if(level) url += `&level=${level}`;
    const res = await fetch(url); 
    const data = await res.json(); 
    document.getElementById('container-logs').innerText = data.logs || '无日志'; 
}
async function runConsole() { const cmd = document.getElementById('console-cmd').value; if(!cmd)return; const res = await fetch('/api/console', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, cmd}) }); const data = await res.json(); document.getElementById('console-output').innerText += `> ${cmd}\n${data.output}\n`; }
async function loadBackups() { const res = await fetch(`/api/backups/${currentContainer}`); const data = await res.json(); const l = document.getElementById('backup-list'); l.innerHTML = ''; data.forEach(f => l.innerHTML+=`<tr><td>${f.name}</td><td>${f.size}</td><td><button class="btn btn-sm btn-warning py-0 me-1" onclick="restoreBackup('${f.name}')">复</button><button class="btn btn-sm btn-outline-danger py-0" onclick="deleteBackup('${f.name}')">删</button></td></tr>`); loadCron(); loadBackupSchedule(); }

async function loadBackupSchedule() {
    if(!currentContainer) return;
    try {
        const res = await fetch('/api/backup/schedule');
        const schedule = await res.json();
        const config = schedule[currentContainer] || {};
        document.getElementById('backup-interval').value = config.interval || 1;
        document.getElementById('backup-keep').value = config.keep_days || 7;
        document.getElementById('backup-enabled').checked = config.enabled || false;
        document.getElementById('last-backup').innerText = config.last_backup || '-';
        document.getElementById('backup-status').innerText = config.last_result || '-';
    } catch(e) { console.error('Failed to load backup schedule:', e); }
}

async function saveBackupSchedule() {
    if(!currentContainer) return;
    const interval = parseInt(document.getElementById('backup-interval').value) || 1;
    const keep_days = parseInt(document.getElementById('backup-keep').value) || 7;
    const enabled = document.getElementById('backup-enabled').checked;
    const res = await fetch('/api/backup/schedule', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({container: currentContainer, interval, keep_days, enabled}) });
    const data = await res.json();
    if(data.status === 'ok') { alert('备份策略已保存'); loadBackupSchedule(); } else { alert('保存失败: ' + data.error); }
}

async function deleteBackupSchedule() {
    if(!currentContainer) return;
    if(!confirm('确定删除此容器的备份策略?')) return;
    const res = await fetch(`/api/backup/schedule/${currentContainer}`, { method: 'DELETE' });
    const data = await res.json();
    if(data.status === 'ok') { document.getElementById('backup-interval').value = 1; document.getElementById('backup-keep').value = 7; document.getElementById('backup-enabled').checked = false; document.getElementById('last-backup').innerText = '-'; document.getElementById('backup-status').innerText = '-'; alert('备份策略已删除'); }
}

async function deleteBackup(f) { if(!confirm(`确认删除 ${f}?`))return; const res = await fetch('/api/backups/delete', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, file:f}) }); if((await res.json()).status==='ok') loadBackups(); }
async function loadCron() { const res = await fetch(`/api/cron/${currentContainer}`); const data = await res.json(); document.getElementById('cron-expr').value = data.expression || ''; }
async function saveCron() { const e = document.getElementById('cron-expr').value; if(!e)return; const res = await fetch('/api/cron', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, expression:e}) }); if((await res.json()).status==='ok') alert('已保存'); }
async function removeCron() { if(!confirm('确认删除定时任务?'))return; const res = await fetch('/api/cron/delete', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer}) }); if((await res.json()).status==='ok') { document.getElementById('cron-expr').value=''; alert('已删除'); } }
async function doBackup() { const res = await fetch('/api/action', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, action:'backup'}) }); const data = await res.json(); bootstrap.Modal.getInstance(document.getElementById('editModal')).hide(); viewTask(data.task_id); }
async function restoreBackup(f) { if(!confirm('确认还原?'))return; const res = await fetch('/api/restore', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, file:f}) }); const data = await res.json(); bootstrap.Modal.getInstance(document.getElementById('editModal')).hide(); viewTask(data.task_id); }

async function showSettings() { const res = await fetch('/api/settings'); const data = await res.json(); const f = document.getElementById('settingsForm'); f.bin.value = data.bin; f.root.value = data.root; f.mirror.value = data.mirror; f.use_mirror.checked = data.use_mirror; new bootstrap.Modal(document.getElementById('settingsModal')).show(); }
async function saveSettings() { const f = document.getElementById('settingsForm'); await fetch('/api/settings', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({bin:f.bin.value, root:f.root.value, mirror:f.mirror.value, use_mirror:f.use_mirror.checked}) }); bootstrap.Modal.getInstance(document.getElementById('settingsModal')).hide(); }

let currentPath = '/';
async function loadFiles(p) {
    if(p === '..') currentPath = currentPath.split('/').slice(0,-1).join('/') || '/';
    else if(p !== '/') currentPath = (currentPath === '/' ? '' : currentPath) + '/' + p;
    else currentPath = '/';
    document.getElementById('file-path').value = currentPath;
    const res = await fetch('/api/files/list', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, path:currentPath}) });
    const data = await res.json(); const tbody = document.getElementById('file-list'); tbody.innerHTML = '';
    if(data.error) { alert(data.error); return; }
    data.forEach(f => {
        const icon = f.type === 'dir' ? '<i class="bi bi-folder-fill text-warning"></i>' : '<i class="bi bi-file-earmark-text"></i>';
        const action = f.type === 'dir' ? `<button class="btn btn-sm btn-outline-info py-0" onclick="loadFiles('${escapeHtml(f.name)}')">进入</button>` : `<button class="btn btn-sm btn-outline-light py-0" onclick="editFile('${escapeHtml(f.name)}')">编辑</button>`;
        tbody.innerHTML += `<tr><td>${icon} ${escapeHtml(f.name)}</td><td>${escapeHtml(f.size)}</td><td>${action}</td></tr>`;
    });
}
async function editFile(n) { const p = (currentPath==='/'?'':currentPath)+'/'+n; const res = await fetch('/api/files/read', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, path:p}) }); const d = await res.json(); if(d.error){alert(d.error);return;} document.getElementById('file-browser').style.display='none'; document.getElementById('file-editor').style.display='block'; document.getElementById('editing-filename').innerText=n; document.getElementById('file-content').value=d.content; document.getElementById('file-content').dataset.path=p; }
async function saveFile() { const p = document.getElementById('file-content').dataset.path; const c = document.getElementById('file-content').value; const res = await fetch('/api/files/save', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:currentContainer, path:p, content:c}) }); if((await res.json()).status==='ok') alert('保存成功'); else alert('保存失败'); }
function closeEditor() { document.getElementById('file-editor').style.display='none'; document.getElementById('file-browser').style.display='block'; }

loadContainers();

// 移动端导航
function showMobilePage(page) {
    document.querySelectorAll('.page-section').forEach(p => p.style.display = 'none');
    document.getElementById(page).style.display = 'block';
    document.querySelectorAll('.mobile-nav-btn').forEach(b => b.classList.remove('active'));
    event.target.closest('.mobile-nav-btn')?.classList.add('active');
}
</script>
</body></html>
"""

# === 后端 API ===
def get_config_dict():
    cfg = {"bin": "/usr/local/bin/rurima", "root": "/root/container", "mirror": "docker.1ms.run", "use_mirror": True}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            c = f.read()
            m_bin = re.search(r'RURIMA_BIN="(.*?)"', c); cfg['bin'] = m_bin.group(1) if m_bin else cfg['bin']
            m_root = re.search(r'DEFAULT_CONTAINER_ROOT="(.*?)"', c); cfg['root'] = m_root.group(1) if m_root else cfg['root']
            m_mirror = re.search(r'DOCKER_MIRROR="(.*?)"', c); cfg['mirror'] = m_mirror.group(1) if m_mirror else cfg['mirror']
            m_use = re.search(r'USE_MIRROR="(.*?)"', c); cfg['use_mirror'] = (m_use.group(1) == 'true') if m_use else True
    return cfg
def get_container_info(n):
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            for l in f:
                if l.startswith(f"CONTAINER|{n}|"): return l.strip().split('|')[3], l.strip().split('|')[4]
    return None, None

def validate_container_path(name, rel_path):
    path, _ = get_container_info(name)
    if not path:
        return None

    abs_root = os.path.abspath(path)

    # 使用safe_path_join进行安全路径拼接
    target = safe_path_join(abs_root, rel_path)
    if target is None:
        return None

    # 检测符号链接攻击
    if os.path.islink(target):
        link_target = os.path.abspath(os.path.join(os.path.dirname(target), os.readlink(target)))
        if not link_target.startswith(abs_root + os.sep):
            return None

    return target

def get_listening_ports_map():
    mapping = {}
    for f in ['/proc/net/tcp', '/proc/net/tcp6']:
        if not os.path.exists(f): continue
        with open(f, 'r') as file:
            next(file)
            for line in file:
                parts = line.split()
                if len(parts) < 10 or parts[3] != '0A': continue # 0A is LISTEN
                try: mapping[parts[9]] = int(parts[1].split(':')[1], 16)
                except: pass
    return mapping

def get_service_pids(svc):
    try:
        out = subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", svc], text=True).strip()
        if not out or out == "0": return []
        cgroup = subprocess.check_output(["systemctl", "show", "-p", "ControlGroup", "--value", svc], text=True).strip()
        procs_path = os.path.join("/sys/fs/cgroup", cgroup.lstrip('/'), "cgroup.procs")
        if not os.path.exists(procs_path): procs_path = os.path.join("/sys/fs/cgroup/systemd", cgroup.lstrip('/'), "cgroup.procs")
        if os.path.exists(procs_path):
            with open(procs_path, 'r') as f: return [p.strip() for p in f.readlines()]
        return [out]
    except: return []

def get_ports_for_pids(pids, inode_map):
    ports = set()
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        if not os.path.exists(fd_dir): continue
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                    if target.startswith("socket:["):
                        inode = target[8:-1]
                        if inode in inode_map: ports.add(inode_map[inode])
                except: pass
        except: pass
    return sorted(list(ports))

def get_docker_config_from_tar(tar_path):
    cmd = None; workdir = None; envs = []; error = None
    try:
        with tarfile.open(tar_path, 'r') as t:
            try:
                t.getmember('manifest.json')
            except KeyError:
                return None, None, "未检测到 manifest.json。这可能是 'docker export' 格式的包，不包含启动配置，请手动填写。"
            try:
                # 1. 读取 manifest.json 获取配置文件名
                m = json.load(t.extractfile('manifest.json'))
                config_filename = m[0]['Config']
                # 2. 读取配置文件获取 Cmd/Entrypoint 和 WorkingDir
                c = json.load(t.extractfile(config_filename))
                cfg = c.get('config', {})
                entrypoint = cfg.get('Entrypoint') or []
                default_cmd = cfg.get('Cmd') or []
                cmd_list = (entrypoint + default_cmd) if entrypoint else default_cmd
                if cmd_list: cmd = ' '.join(shlex.quote(s) for s in cmd_list)
                workdir = cfg.get('WorkingDir')
                envs = cfg.get('Env') or []
            except Exception as e: error = f"解析配置失败: {str(e)}"
    except Exception as e: error = f"无法读取文件: {str(e)}"
    return cmd, workdir, envs, error

@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE)

@app.route('/api/login', methods=['POST'])
def login():
    provided_key = request.json.get('key', '')
    if constant_time_compare(provided_key, API_KEY):
        return jsonify({"status": "ok"})
    else:
        return jsonify({"error": "Invalid"}), 401

@app.before_request
def check_auth():
    if request.path.startswith('/api/') and request.path != '/api/login' and request.headers.get('X-API-Key') != API_KEY: return jsonify({"error": "Unauthorized"}), 401

# === 资源监控 API ===
def get_container_stats(container_name):
    """获取容器资源使用统计"""
    path, svc = get_container_info(container_name)
    if not path:
        return None
    
    stats = {
        "cpu": 0,
        "memory": {"used": 0, "total": 0, "percent": 0},
        "network": {"rx": 0, "tx": 0},
        "disk": {"used": 0, "total": 0},
        "processes": 0
    }
    
    try:
        # 获取 systemd 服务进程
        result = subprocess.run(["systemctl", "show", svc, "--property=MainPID"], 
                              capture_output=True, text=True)
        pid_str = result.stdout.split("=")[1].strip()
        
        if pid_str.isdigit() and int(pid_str) > 0:
            pid = int(pid_str)
            
            try:
                # 尝试获取主进程和子进程的CPU/内存
                def get_process_tree_cpu_mem(parent_pid, container_name):
                    total_cpu = 0
                    total_mem = 0
                    try:
                        # 方法1: 通过父PID获取子进程
                        result = subprocess.run(["ps", "--ppid", str(parent_pid), "-o", "pid,pcpu,rss,comm", "--no-headers"], 
                                            capture_output=True, text=True)
                        for line in result.stdout.strip().split('\n'):
                            if line.strip():
                                parts = line.split()
                                if len(parts) >= 3:
                                    try:
                                        total_cpu += float(parts[1])
                                        total_mem += int(parts[2]) * 1024
                                    except:
                                        pass
                        
                        # 方法2: 也获取主进程
                        main_result = subprocess.run(["ps", "-p", str(parent_pid), "-o", "pid,pcpu,rss,comm", "--no-headers"], 
                                                  capture_output=True, text=True)
                        for line in main_result.stdout.strip().split('\n'):
                            if line.strip():
                                parts = line.split()
                                if len(parts) >= 3:
                                    try:
                                        total_cpu += float(parts[1])
                                        total_mem += int(parts[2]) * 1024
                                    except:
                                        pass
                        
                        # 方法3: 通过进程名匹配 (更准确)
                        # 匹配容器名相关的进程
                        name_patterns = [container_name.lower()]
                        if container_name == 'qbittorrent':
                            name_patterns.append('qbittorrent')
                        elif container_name == 'transmission':
                            name_patterns.append('transmission')
                        elif 'bait' in container_name.lower():
                            name_patterns.append('baitts')
                        elif 'openlist' in container_name.lower():
                            name_patterns.append('openlist')
                        elif 'moviepilot' in container_name.lower():
                            name_patterns.append('moviepilot')
                        
                        all_procs = subprocess.run(["ps", "aux"], capture_output=True, text=True)
                        for line in all_procs.stdout.split('\n'):
                            for pattern in name_patterns:
                                if pattern in line.lower():
                                    parts = line.split()
                                    if len(parts) >= 3:
                                        try:
                                            # 跳过标题行
                                            if parts[2] == '%CPU':
                                                continue
                                            total_cpu += float(parts[2])
                                            total_mem += int(parts[5]) * 1024  # RSS in KB
                                        except:
                                            pass
                                    break
                                    
                    except:
                        pass
                    return total_cpu, total_mem
                
                cpu, mem = get_process_tree_cpu_mem(pid, container_name)
                stats["cpu"] = cpu
                stats["memory"]["used"] = mem
                if mem > 0:
                    try:
                        stats["memory"]["total"] = psutil.virtual_memory().total
                        stats["memory"]["percent"] = (mem / stats["memory"]["total"]) * 100
                    except:
                        pass
                
                # 子进程数
                try:
                    process = psutil.Process(pid)
                    stats["processes"] = len(process.children(recursive=True)) + 1
                except:
                    pass
                
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # 网络 I/O (尝试从 /proc/net/dev 获取)
        try:
            with open("/proc/net/dev", "r") as f:
                lines = f.readlines()[2:]  # 跳过表头
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 17:
                        # 简化处理，累加所有接口
                        stats["network"]["rx"] += int(parts[1])
                        stats["network"]["tx"] += int(parts[9])
        except:
            pass
        
        # 磁盘使用
        try:
            result = subprocess.run(["du", "-sb", path], 
                                  capture_output=True, text=True)
            if result.returncode == 0:
                stats["disk"]["used"] = int(result.stdout.split()[0])
        except:
            pass
            
    except Exception as e:
        logging.error(f"Failed to get stats for {container_name}: {e}")
    
    return stats

@app.route('/api/stats/<container_name>')
def container_stats_api(container_name):
    """获取单个容器统计"""
    if not validate_container_name(container_name):
        return jsonify({"error": "Invalid container name"}), 400
    
    stats = get_container_stats(container_name)
    if stats is None:
        return jsonify({"error": "Container not found"}), 404
    
    return jsonify(stats)

@app.route('/api/stats')
def all_stats_api():
    """获取所有容器统计"""
    # 获取容器列表名称
    container_names = []
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                if line.startswith("CONTAINER|"):
                    parts = line.strip().split("|")
                    container_names.append(parts[1])
    
    result = {}
    for name in container_names:
        stats = get_container_stats(name)
        result[name] = stats
    return jsonify(result)

@app.route('/api/tasks')
def list_tasks_api():
    l = []
    for tid, t in TASKS.items():
        meta = t.get('meta', {})
        l.append({"id": tid, "name": meta.get('name', 'Unknown'), "type": meta.get('type', 'Unknown'), "status": t['status'], "time": meta.get('time', '')})
    return jsonify(list(reversed(l)))

@app.route('/api/containers')
def list_containers():
    l = []
    inode_map = get_listening_ports_map()
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                if line.startswith("CONTAINER|"):
                    p = line.strip().split("|")
                    st = "STOPPED"
                    ports = []
                    if subprocess.run(["systemctl", "is-active", "--quiet", p[4]]).returncode == 0:
                        st = "RUNNING"
                        ports = get_ports_for_pids(get_service_pids(p[4]), inode_map)
                    l.append({"name": p[1], "image": p[2], "path": p[3], "status": st, "ports": ports})
    return jsonify(l)
@app.route('/api/action', methods=['POST'])
def action():
    d = request.json
    name = d.get('name', '')
    action_type = d.get('action', '')

    # 验证容器名称
    if not validate_container_name(name):
        return jsonify({"error": "Invalid container name"}), 400

    # 验证操作类型
    allowed_actions = {'start', 'stop', 'restart', 'rm', 'backup'}
    if action_type not in allowed_actions:
        return jsonify({"error": "Invalid action"}), 400

    tid = str(uuid.uuid4())
    # 使用列表参数构建命令，避免shell注入
    if action_type == 'rm':
        cmd = [RUMA_BIN, "rm", name, "-y"]
    else:
        cmd = [RUMA_BIN, action_type, name]

    threading.Thread(target=run_background_task, args=(tid, cmd, {'type': action_type, 'name': name, 'time': time.strftime('%H:%M:%S')})).start()
    return jsonify({"task_id": tid})
@app.route('/api/update', methods=['POST'])
def update_container_api():
    d = request.json
    name = d.get('name', '')

    # 验证容器名称
    if not validate_container_name(name):
        return jsonify({"error": "Invalid container name"}), 400

    tid = str(uuid.uuid4())
    threading.Thread(target=run_background_task, args=(tid, [RUMA_BIN, "update", name, "-y"], {'type': 'update', 'name': name, 'time': time.strftime('%H:%M:%S')})).start()
    return jsonify({"task_id": tid})
@app.route('/api/task/<tid>')
def task_status(tid): t=TASKS.get(tid, {"status": "error", "logs": ["Not found"]}); return jsonify({"status": t['status'], "logs": t['logs'], "meta": t.get('meta', {})})

@app.route('/api/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'GET': return jsonify(get_config_dict())
    d = request.json
    lines = []
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f: lines = [l for l in f.readlines() if not any(l.startswith(k) for k in ["RURIMA_BIN=","DEFAULT_CONTAINER_ROOT=","DOCKER_MIRROR=","USE_MIRROR="])]
    with open(CONFIG_FILE, 'w') as f:
        f.write(f'RURIMA_BIN="{d["bin"]}"\nDEFAULT_CONTAINER_ROOT="{d["root"]}"\nDOCKER_MIRROR="{d["mirror"]}"\nUSE_MIRROR="{"true" if d["use_mirror"] else "false"}"\n')
        f.writelines(lines)
    return jsonify({"success": True})

@app.route('/api/config/details/<name>')
def config_details(name):
    path, svc = get_container_info(name)
    details = {"image": "unknown", "autorun": False, "envs": [], "mounts": [], "cmd": "/init", "workdir": "", "extra_flags": []}
    if subprocess.run(["systemctl", "is-enabled", "--quiet", svc]).returncode == 0: details["autorun"] = True
    svc_file = f"/etc/systemd/system/{svc}.service"
    if os.path.exists(svc_file):
        with open(svc_file, 'r') as f:
            for l in f:
                if l.strip().startswith("ExecStart="):
                    try:
                        args = shlex.split(l.strip().replace("ExecStart=", ""))
                        i = 0
                        while i < len(args):
                            if args[i] == '-e':
                                if i+1<len(args) and '=' in args[i+1]: k,v=args[i+1].split('=',1); details["envs"].append({"key":k,"val":v}); i+=2; continue
                                if i+2<len(args): details["envs"].append({"key":args[i+1],"val":args[i+2]}); i+=3; continue
                            if args[i] in ['-m','-v','-M']:
                                if i+1<len(args) and ':' in args[i+1]: s,t=args[i+1].split(':',1); details["mounts"].append({"src":s,"tgt":t}); i+=2; continue
                                if i+2<len(args): details["mounts"].append({"src":args[i+1],"tgt":args[i+2]}); i+=3; continue
                            if args[i] == '-W':
                                if i+1<len(args): details["workdir"] = args[i+1]; i+=2; continue
                            if args[i] == '-w':
                                details["extra_flags"].append("-w"); i+=1; continue
                            i+=1
                        if path in args:
                            idx = args.index(path)
                            if idx + 1 < len(args): details["cmd"] = ' '.join(args[idx+1:])
                            else: details["cmd"] = ""
                    except: pass
                    break
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            for l in f:
                if l.startswith(f"CONTAINER|{name}|"): details["image"] = l.split('|')[2]; break
    return jsonify(details)

@app.route('/api/config/save_complex', methods=['POST'])
def save_complex():
    d = request.json; name = d['name']; old_name = d.get('old_name')
    
    # 处理重命名逻辑
    if old_name and name and old_name != name:
        if get_container_info(name)[0]: return jsonify({"error": f"Container {name} already exists"}), 400
        old_path, old_svc = get_container_info(old_name)
        if not old_path: return jsonify({"error": "Container not found"}), 404
        
        subprocess.run(["systemctl", "stop", old_svc], stderr=subprocess.DEVNULL)
        subprocess.run(["systemctl", "disable", old_svc], stderr=subprocess.DEVNULL)
        
        cfg = get_config_dict(); new_path = os.path.join(cfg['root'], name)
        if os.path.exists(old_path):
            subprocess.run(["umount", "-l", old_path], stderr=subprocess.DEVNULL)
            os.rename(old_path, new_path)
        elif not os.path.exists(new_path): os.makedirs(new_path)
        
        # 更新配置文件
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f: lines = f.readlines()
            with open(CONFIG_FILE, 'w') as f:
                for l in lines:
                    if l.startswith(f"CONTAINER|{old_name}|"):
                        p = l.strip().split('|'); f.write(f"CONTAINER|{name}|{p[2]}|{new_path}|{name}\n")
                    else: f.write(l)
        
        # 重命名服务文件
        old_svc_file = f"/etc/systemd/system/{old_svc}.service"; new_svc_file = f"/etc/systemd/system/{name}.service"
        if os.path.exists(old_svc_file):
            with open(old_svc_file, 'r') as f: c = f.read().replace(f"Description=Rurima Container - {old_name}", f"Description=Rurima Container - {name}").replace(old_path, new_path)
            with open(new_svc_file, 'w') as f: f.write(c)
            os.remove(old_svc_file)
        subprocess.run(["systemctl", "daemon-reload"], stderr=subprocess.DEVNULL)

    subprocess.run(["systemctl", "enable" if d['autorun'] else "disable", name])
    cfg = get_config_dict(); path, svc = get_container_info(name)
    cmd = [cfg['bin'], "r", "-u"]
    if d.get('extra_flags'): cmd.extend(d['extra_flags'])
    if d.get('workdir'): cmd.extend(["-W", d['workdir']])
    if not any(m['src'] == '/root/resolv.conf' for m in d['mounts']): cmd.extend(["-M", "/root/resolv.conf", "/etc/resolv.conf"])
    for m in d['mounts']: cmd.extend(["-m", m['src'], m['tgt']])
    for e in d['envs']: cmd.extend(["-e", e['key'], e['val']])
    cmd.extend([path]); cmd.extend(shlex.split(d.get('cmd', '/init')))
    final = ' '.join(shlex.quote(s) for s in cmd)
    svc_file = f"/etc/systemd/system/{svc}.service"
    lines = []
    with open(svc_file, 'r') as f:
        for l in f: lines.append(f"ExecStart={final}\n" if l.strip().startswith("ExecStart=") else l)
    with open(svc_file, 'w') as f: f.writelines(lines)
    tid = str(uuid.uuid4())
    threading.Thread(target=run_background_task, args=(tid, ["bash", "-c", f"systemctl daemon-reload && systemctl restart {svc}"], {'type': 'config', 'name': name, 'time': time.strftime('%H:%M:%S')})).start()
    return jsonify({"task_id": tid})

@app.route('/api/install', methods=['POST'])
def install():
    d = request.json
    # 如果 WebUI 已经拼好了带镜像源的 url，这里直接用，否则在 ruma 脚本里也会处理
    cmd = [RUMA_BIN, "run", "-u", "--autorun", "y" if d['autorun'] else "n"]
    for m in d.get('mounts', []): cmd.extend(["-v", f"{m['src']}:{m['tgt']}"])
    for e in d.get('envs', []): cmd.extend(["-e", f"{e['key']}={e['val']}"])
    name = d.get('name')
    if name: cmd.extend(["--name", name])
    cmd.append(d['image'])
    name = name or d['image'].split('/')[-1].split(':')[0]
    tid = str(uuid.uuid4())
    threading.Thread(target=run_background_task, args=(tid, cmd, {'type': 'install', 'name': name, 'time': time.strftime('%H:%M:%S')})).start()
    return jsonify({"task_id": tid})

@app.route('/api/logs/<n>')
def logs(n):
    _,s=get_container_info(n); 
    if not s: s = n
    
    # 获取查询参数
    search = request.args.get('search', '')
    level = request.args.get('level', '')  # info, warn, error
    lines = request.args.get('lines', '200')
    
    # 获取日志
    res = subprocess.run(["journalctl","-u",s,"-n",lines,"--no-pager"],capture_output=True,text=True)
    logs_text = res.stdout
    
    # 过滤
    filtered_logs = []
    for line in logs_text.split('\n'):
        if not line.strip():
            continue
        
        # 级别过滤
        line_upper = line.upper()
        if level == 'error' and 'ERROR' not in line_upper and 'ERR' not in line_upper:
            continue
        if level == 'warn' and 'WARN' not in line_upper and 'WARNING' not in line_upper:
            continue
        if level == 'info' and 'INFO' not in line_upper:
            # 不一定所有日志都有 INFO，简单处理
            if 'ERROR' not in line_upper and 'WARN' not in line_upper:
                pass  # 保留其他行
            else:
                continue
        
        # 关键词搜索
        if search and search.lower() not in line.lower():
            continue
        
        filtered_logs.append(line)
    
    return jsonify({"logs": '\n'.join(filtered_logs), "total": len(filtered_logs)})
@app.route('/api/console', methods=['POST'])
def console():
    d = request.json
    name = d.get('name', '')

    # 验证容器名称
    if not validate_container_name(name):
        return jsonify({"error": "Invalid container name"}), 400

    path, _ = get_container_info(name)
    if not path:
        return jsonify({"error": "Container not found"}), 400

    cfg = get_config_dict()

    # 验证并清理命令参数
    cmd_input = d.get('cmd', '')
    try:
        cmd_args = shlex.split(cmd_input)
    except ValueError as e:
        return jsonify({"error": f"Invalid command: {str(e)}"}), 400

    # 验证每个参数
    for arg in cmd_args:
        if not validate_command_arg(arg):
            return jsonify({"error": "Invalid characters in command"}), 400

    cmd = [cfg["bin"], "r", "-u", path] + cmd_args
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return jsonify({"output": res.stdout + res.stderr})
@app.route('/api/backups/<n>')
def backups(n): return jsonify([{"name":f,"size":f"{os.path.getsize(os.path.join(BACKUP_DIR,f))/1024/1024:.2f}MB"} for f in os.listdir(BACKUP_DIR) if f.startswith(n+"_")])
@app.route('/api/restore', methods=['POST'])
def restore():
    d = request.json
    container_name = d.get('name', '')
    filename = d.get('file', '')

    # 验证容器名称
    if not validate_container_name(container_name):
        return jsonify({"error": "Invalid container name"}), 400

    # 验证文件名安全
    safe_name = safe_filename(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    # 验证备份文件存在且属于该容器
    backup_path = os.path.join(BACKUP_DIR, safe_name)
    if not os.path.exists(backup_path):
        return jsonify({"error": "Backup file not found"}), 400

    # 验证文件名以容器名开头
    if not safe_name.startswith(container_name + "_"):
        return jsonify({"error": "Backup file does not match container"}), 400

    # 使用列表参数构建命令
    cmd = [RUMA_BIN, "restore", container_name, backup_path]

    tid = str(uuid.uuid4())
    threading.Thread(target=run_background_task, args=(tid, cmd, {'type': 'restore', 'name': container_name, 'time': time.strftime('%H:%M:%S')})).start()
    return jsonify({"task_id": tid})

@app.route('/api/files/list', methods=['POST'])
def file_list():
    t = validate_container_path(request.json['name'], request.json.get('path', '/'))
    if not t or not os.path.isdir(t): return jsonify({"error": "Invalid path"}), 400
    try: l = [{"name":f,"type":"dir" if os.path.isdir(os.path.join(t,f)) else "file","size":os.stat(os.path.join(t,f)).st_size} for f in os.listdir(t)]; l.sort(key=lambda x:(0 if x['type']=='dir' else 1, x['name'])); return jsonify(l)
    except Exception as e: return jsonify({"error": str(e)}), 500
@app.route('/api/files/read', methods=['POST'])
def file_read():
    # 先验证路径
    name = request.json.get('name', '')
    path = request.json.get('path', '')

    if not validate_container_name(name):
        return jsonify({"error": "Invalid container name"}), 400

    t = validate_container_path(name, path)
    if not t or not os.path.isfile(t):
        return jsonify({"error": "Invalid path"}), 400

    # 先检查大小再读取，防止大文件攻击
    try:
        file_size = os.path.getsize(t)
        if file_size > MAX_FILE_SIZE:
            return jsonify({"error": "File too large"}), 400

        with open(t, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return jsonify({"content": content})
    except Exception as e:
        logging.error(f"File read error: {e}")
        return jsonify({"error": str(e)}), 500
@app.route('/api/files/save', methods=['POST'])
def file_save():
    name = request.json.get('name', '')
    path = request.json.get('path', '')
    content = request.json.get('content', '')

    # 验证容器名称
    if not validate_container_name(name):
        return jsonify({"error": "Invalid container name"}), 400

    # 第一次验证
    t = validate_container_path(name, path)
    if not t:
        return jsonify({"error": "Invalid path"}), 400

    # 第二次验证 - 确保路径没有变化
    t2 = validate_container_path(name, path)
    if t != t2:
        return jsonify({"error": "Path validation failed"}), 400

    # 写入文件
    try:
        with open(t, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"status": "ok"})
    except Exception as e:
        logging.error(f"File save error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/backups/delete', methods=['POST'])
def delete_backup_api():
    d = request.json
    container_name = d.get('name', '')
    filename = d.get('file', '')

    # 验证容器名称
    if not validate_container_name(container_name):
        return jsonify({"error": "Invalid container name"}), 400

    # 验证文件名安全
    safe_name = safe_filename(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    # 严格验证文件名格式：容器名_时间戳.tar.gz
    if not safe_name.startswith(container_name + "_") or not safe_name.endswith('.tar.gz'):
        return jsonify({"error": "Invalid backup file format"}), 400

    # 验证完整路径
    backup_path = os.path.join(BACKUP_DIR, safe_name)
    if not safe_path_join(BACKUP_DIR, safe_name):
        return jsonify({"error": "Invalid path"}), 400

    if os.path.exists(backup_path):
        try:
            os.remove(backup_path)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"status": "ok"})

# === 备份策略 API ===
@app.route('/api/backup/schedule', methods=['GET'])
def get_backup_schedule():
    """获取备份调度配置"""
    schedule = load_backup_schedule()
    return jsonify(schedule)

@app.route('/api/backup/schedule', methods=['POST'])
def set_backup_schedule():
    """设置备份调度"""
    d = request.json
    container_name = d.get('container', '')
    interval = d.get('interval', 1)  # 天
    keep_days = d.get('keep_days', 7)
    enabled = d.get('enabled', False)
    
    if not validate_container_name(container_name):
        return jsonify({"error": "Invalid container name"}), 400
    
    schedule = load_backup_schedule()
    schedule[container_name] = {
        "interval": interval,
        "keep_days": keep_days,
        "enabled": enabled,
        "last_backup": schedule.get(container_name, {}).get('last_backup', ''),
        "last_result": schedule.get(container_name, {}).get('last_result', '')
    }
    save_backup_schedule(schedule)
    
    return jsonify({"status": "ok"})

@app.route('/api/backup/schedule/<container_name>', methods=['DELETE'])
def delete_backup_schedule(container_name):
    """删除备份调度"""
    if not validate_container_name(container_name):
        return jsonify({"error": "Invalid container name"}), 400
    
    schedule = load_backup_schedule()
    if container_name in schedule:
        del schedule[container_name]
        save_backup_schedule(schedule)
    
    return jsonify({"status": "ok"})

@app.route('/api/backup/now/<container_name>', methods=['POST'])
def backup_now(container_name):
    """立即备份"""
    if not validate_container_name(container_name):
        return jsonify({"error": "Invalid container name"}), 400
    
    success, result = run_backup(container_name)
    if success:
        # 更新最后备份时间
        schedule = load_backup_schedule()
        if container_name in schedule:
            schedule[container_name]['last_backup'] = time.strftime("%Y%m%d_%H%M%S")
            schedule[container_name]['last_result'] = 'ok'
            save_backup_schedule(schedule)
        return jsonify({"status": "ok", "file": result})
    else:
        return jsonify({"error": result}), 500

@app.route('/api/cron/<name>')
def get_cron(name):
    try: l=subprocess.check_output(["crontab","-l"],text=True).splitlines()
    except: l=[]
    return jsonify({"expression": next((x.split(f"{RUMA_BIN} backup {name}")[0].strip() for x in l if f"{RUMA_BIN} backup {name}" in x and not x.strip().startswith('#')), "")})
@app.route('/api/cron', methods=['POST'])
def save_cron():
    d=request.json; n=d['name']; e=d['expression'].strip()
    try: l=subprocess.check_output(["crontab","-l"],text=True).splitlines()
    except: l=[]
    nl=[x for x in l if f"{RUMA_BIN} backup {n}" not in x]; nl.append(f"{e} {RUMA_BIN} backup {n} >> {os.path.join(BACKUP_DIR,n+'.log')} 2>&1"); subprocess.run(["crontab","-"],input="\n".join(nl)+"\n",text=True); return jsonify({"status":"ok"})
@app.route('/api/cron/delete', methods=['POST'])
def delete_cron():
    d=request.json; n=d['name']
    try: l=subprocess.check_output(["crontab","-l"],text=True).splitlines()
    except: l=[]
    nl=[x for x in l if f"{RUMA_BIN} backup {n}" not in x]; subprocess.run(["crontab","-"],input="\n".join(nl)+"\n",text=True); return jsonify({"status":"ok"})

@app.route('/api/import', methods=['POST'])
def import_container_api():
    name = request.form['name']; cmd_arg = request.form.get('cmd', '/init'); workdir_arg = request.form.get('workdir', ''); autorun = request.form.get('autorun') == 'true'
    envs = json.loads(request.form.get('envs', '[]')); mounts = json.loads(request.form.get('mounts', '[]'))
    
    local_file = request.form.get('local_file')
    if local_file and os.path.exists(local_file) and local_file.startswith(BACKUP_DIR):
        tmp_path = local_file
    else:
        if 'file' not in request.files: return jsonify({"error": "No file"}), 400
        f = request.files['file']; tmp_path = os.path.join(BACKUP_DIR, f"import_{name}_{uuid.uuid4()}.tar.gz")
        f.save(tmp_path)
    
    # 如果用户未指定命令或使用默认值，且未手动指定工作目录，尝试从 tar 包中提取配置 (Fallback)
    detected_envs = []
    if cmd_arg == '/init' and not workdir_arg:
        detected_cmd, detected_workdir, detected_envs, _ = get_docker_config_from_tar(tmp_path)
        if detected_cmd: cmd_arg = detected_cmd
        if detected_workdir: workdir_arg = detected_workdir

    cmd = [RUMA_BIN, "import", "-f", tmp_path, "-n", name, "-c", cmd_arg, "--autorun", "y" if autorun else "n"]
    if workdir_arg: cmd.extend(["-W", workdir_arg])
    for m in mounts: cmd.extend(["-v", f"{m['src']}:{m['tgt']}"])
    for e in envs: cmd.extend(["-e", f"{e['key']}={e['val']}"])
    
    # 如果是自动检测模式，补充缺失的环境变量
    if detected_envs:
        user_keys = {e['key'] for e in envs}
        for env_str in detected_envs:
            if '=' in env_str:
                k, v = env_str.split('=', 1)
                if k not in user_keys: cmd.extend(["-e", f"{k}={v}"])

    tid = str(uuid.uuid4()); threading.Thread(target=run_background_task, args=(tid, cmd, {'type': 'import', 'name': name, 'time': time.strftime('%H:%M:%S')})).start(); return jsonify({"task_id": tid})

@app.route('/api/import/parse', methods=['POST'])
def parse_import_tar():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    f = request.files['file']; tmp_path = os.path.join(BACKUP_DIR, f"upload_temp_{uuid.uuid4()}.tar.gz"); f.save(tmp_path)
    cmd, workdir, envs, error = get_docker_config_from_tar(tmp_path)
    if error: return jsonify({"error": error})
    return jsonify({"status": "ok", "cmd": cmd or "/init", "workdir": workdir or "", "envs": envs, "path": tmp_path})

@app.route('/api/templates', methods=['GET', 'POST'])
def templates_api():
    if request.method == 'GET':
        return jsonify([f for f in os.listdir(TEMPLATE_DIR) if f.endswith('.yaml')])

    d = request.json
    n = d.get('name')
    c = d.get('content')

    if not n or not c:
        return jsonify({"error": "Missing args"}), 400

    # 验证文件名安全
    safe_name = safe_filename(n)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    # 确保扩展名
    if not safe_name.endswith('.yaml'):
        safe_name += '.yaml'

    # 验证内容不包含路径遍历
    if '..' in c or c.startswith('/'):
        return jsonify({"error": "Invalid content"}), 400

    # 验证路径
    template_path = os.path.join(TEMPLATE_DIR, safe_name)
    if not safe_path_join(TEMPLATE_DIR, safe_name):
        return jsonify({"error": "Invalid path"}), 400

    with open(template_path, 'w') as f:
        f.write(c)
    return jsonify({"status": "ok"})

@app.route('/api/templates/delete', methods=['POST'])
def delete_template_api():
    n = request.json.get('name')

    # 验证文件名
    safe_name = safe_filename(n)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    p = os.path.join(TEMPLATE_DIR, safe_name)

    # 二次验证路径
    if not safe_path_join(TEMPLATE_DIR, safe_name):
        return jsonify({"error": "Invalid path"}), 400

    if n and os.path.exists(p):
        os.remove(p)
    return jsonify({"status": "ok"})
@app.route('/api/templates/read', methods=['POST'])
def read_template_api():
    n = request.json.get('name'); p = os.path.join(TEMPLATE_DIR, n)
    return jsonify({"content": open(p, 'r').read()}) if os.path.exists(p) else (jsonify({"error": "Not found"}), 404)

if __name__ == '__main__': load_api_key(); app.run(host='0.0.0.0', port=PORT, threaded=True)