#!/usr/bin/env python3
"""
Ruma Web Terminal - 独立的终端连接工具
可以直接运行: python3 ruma_terminal.py
或通过 Web 访问: http://<ip>:5778
"""

import os
import sys
import subprocess
import select
import pty
import termios
import tty
import socket
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

PORT = 5778
CONTAINER_NAME = None

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ruma Terminal</title>
    <link href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        html, body { height: 100%; background: #0d0d0d; overflow: hidden; }
        #terminal { height: 100vh; padding: 10px; }
        .header {
            background: #161616;
            padding: 10px 20px;
            border-bottom: 1px solid #2c2c2e;
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .header h1 {
            color: #5e7ce2;
            font-size: 1.2rem;
            font-weight: 600;
        }
        .header select {
            background: #1a1a1a;
            color: #fff;
            border: 1px solid #2c2c2e;
            padding: 5px 10px;
            border-radius: 8px;
        }
        .header button {
            background: linear-gradient(135deg, #5e7ce2, #7c4dff);
            color: #fff;
            border: none;
            padding: 6px 15px;
            border-radius: 8px;
            cursor: pointer;
        }
        .header button:hover {
            box-shadow: 0 4px 15px rgba(94, 124, 226, 0.4);
        }
        .status {
            color: #8e8e93;
            font-size: 0.85rem;
            margin-left: auto;
        }
        .status.connected { color: #34c759; }
        .status.disconnected { color: #ff453a; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🖥️ Ruma Terminal</h1>
        <select id="container-select">
            <option value="">-- 选择容器 --</option>
        </select>
        <button onclick="connect()">连接</button>
        <button onclick="disconnect()">断开</button>
        <span id="status" class="status disconnected">未连接</span>
    </div>
    <div id="terminal"></div>
    
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
    <script>
        const term = new Terminal({
            cursorBlink: true,
            fontSize: 14,
            fontFamily: 'SF Mono, Fira Code, monospace',
            theme: {
                background: '#0d0d0d',
                foreground: '#ffffff',
                cursor: '#5e7ce2'
            },
            rows: 24,
            cols: 80
        });
        
        term.open(document.getElementById('terminal'));
        
        let socket = null;
        let container = null;
        
        // 加载容器列表
        async function loadContainers() {
            try {
                const res = await fetch('/api/containers');
                const data = await res.json();
                const select = document.getElementById('container-select');
                data.forEach(c => {
                    const opt = document.createElement('option');
                    opt.value = c.name;
                    opt.textContent = c.name + ' (' + c.status + ')';
                    select.appendChild(opt);
                });
            } catch(e) {
                console.error('加载容器失败:', e);
            }
        }
        
        function connect() {
            container = document.getElementById('container-select').value;
            if (!container) {
                alert('请先选择一个容器');
                return;
            }
            
            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            socket = new WebSocket(`${protocol}//${location.host}/ws?container=${container}`);
            
            socket.onopen = () => {
                document.getElementById('status').textContent = '已连接: ' + container;
                document.getElementById('status').className = 'status connected';
                term.write('\\r\\n\\x1b[32m已连接到 ' + container + '\\x1b[0m\\r\\n');
            };
            
            socket.onmessage = (e) => {
                term.write(e.data);
            };
            
            socket.onclose = () => {
                document.getElementById('status').textContent = '已断开';
                document.getElementById('status').className = 'status disconnected';
                term.write('\\r\\n\\x1b[31m连接已断开\\x1b[0m\\r\\n');
            };
            
            socket.onerror = (e) => {
                term.write('\\r\\n\\x1b[31m连接错误\\x1b[0m\\r\\n');
            };
            
            // 发送终端输入
            term.onData((data) => {
                if (socket && socket.readyState === WebSocket.OPEN) {
                    socket.send(data);
                }
            });
        }
        
        function disconnect() {
            if (socket) {
                socket.close();
            }
        }
        
        // 自适应窗口大小
        function resize() {
            const cols = Math.floor(term.cols);
            const rows = term.rows;
            if (socket && socket.readyState === WebSocket.OPEN) {
                socket.send('\\x1b[8;' + rows + ';' + cols + 't');
            }
        }
        
        window.addEventListener('resize', resize);
        term.onResize(resize);
        
        // 加载容器
        loadContainers();
    </script>
</body>
</html>
"""

# WebSocket 终端会话管理
class TerminalHandler:
    def __init__(self):
        self.sessions = {}
    
    def create_session(self, container_name):
        """创建新的终端会话"""
        import uuid
        session_id = str(uuid.uuid4())[:8]
        
        # 获取容器路径
        config_file = os.path.expanduser("~/.ruma_config")
        container_path = None
        
        if os.path.exists(config_file):
            with open(config_file) as f:
                for line in f:
                    if line.startswith(f"CONTAINER|{container_name}|"):
                        parts = line.strip().split("|")
                        if len(parts) >= 3:
                            container_path = parts[2]
                        break
        
        if not container_path:
            container_path = f"/root/container/{container_name}"
        
        self.sessions[session_id] = {
            "container": container_name,
            "path": container_path,
            "master_fd": None,
            "process": None
        }
        
        return session_id
    
    def start_shell(self, session_id):
        """启动 shell"""
        import select
        import pty
        import subprocess
        
        session = self.sessions.get(session_id)
        if not session:
            return False
        
        # 创建 PTY
        master_fd, slave_fd = pty.openpty()
        
        # 启动 shell
        process = subprocess.Popen(
            ["/bin/sh"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=session["path"],
            start_new_session=True
        )
        
        os.close(slave_fd)
        
        session["master_fd"] = master_fd
        session["process"] = process
        session["running"] = True
        
        # 启动输出读取线程
        thread = threading.Thread(target=self._read_output, args=(session_id,), daemon=True)
        thread.start()
        
        return True
    
    def _read_output(self, session_id):
        """读取输出"""
        import select
        
        session = self.sessions.get(session_id)
        if not session:
            return
        
        master_fd = session["master_fd"]
        
        while session.get("running"):
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if ready:
                    try:
                        data = os.read(master_fd, 1024)
                        if session.get("ws"):
                            session["ws"].send(data)
                    except:
                        break
            except:
                break
    
    def write(self, session_id, data):
        """写入输入"""
        session = self.sessions.get(session_id)
        if not session or not session.get("master_fd"):
            return
        
        try:
            os.write(session["master_fd"], data.encode())
        except:
            pass
    
    def resize(self, session_id, rows, cols):
        """调整终端大小"""
        import fcntl
        import termios
        
        session = self.sessions.get(session_id)
        if not session or not session.get("master_fd"):
            return
        
        try:
            fcntl.ioctl(session["master_fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except:
            pass
    
    def close(self, session_id):
        """关闭会话"""
        session = self.sessions.pop(session_id, None)
        if session:
            session["running"] = False
            if session.get("master_fd"):
                try:
                    os.close(session["master_fd"])
                except:
                    pass
            if session.get("process"):
                try:
                    session["process"].terminate()
                except:
                    pass

terminal_handler = TerminalHandler()

def get_container_name_from_config():
    """从配置文件获取容器列表"""
    containers = []
    config_file = os.path.expanduser("~/.ruma_config")
    
    if os.path.exists(config_file):
        with open(config_file) as f:
            for line in f:
                if line.startswith("CONTAINER|"):
                    parts = line.strip().split("|")
                    if len(parts) >= 2:
                        containers.append(parts[1])
    
    return containers

# 简单的 HTTP 服务器
class RequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            # 检查是否有容器参数
            parsed = urlparse(self.path)
            # 返回带容器选择的页面
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            
            # 获取容器列表
            containers = get_container_name_from_config()
            container_options = '\n'.join([f'<option value="{c}">{c}</option>' for c in containers])
            
            html = HTML.replace('<option value="">-- 选择容器 --</option>', 
                               f'<option value="">-- 选择容器 ({len(containers)}个) --</option>\n' + container_options)
            
            self.wfile.write(html.encode())
        elif self.path.startswith('/api/containers'):
            # API: 获取容器列表
            containers = get_container_name_from_config()
            # 检查容器状态
            import subprocess
            result = []
            for name in containers:
                try:
                    svc = subprocess.run(["systemctl", "is-active", name], 
                                       capture_output=True, text=True)
                    status = "RUNNING" if svc.returncode == 0 else "STOPPED"
                except:
                    status = "UNKNOWN"
                result.append({"name": name, "status": status})
            
            import json
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        else:
            self.send_error(404)
    
    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), RequestHandler)
    print(f"🌐 Ruma Terminal 启动成功!")
    print(f"   访问地址: http://localhost:{PORT}")
    print(f"   按 Ctrl+C 停止")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 停止服务")
        server.shutdown()

if __name__ == '__main__':
    run_server()
