# -*- coding: utf-8 -*-
"""
局域网传文件 - Windows 收发端

功能：
  1. 手机 → 电脑：扫码上传截图/文件
  2. 电脑 → 电脑：同 WiFi 下填对方 IP，快速互传
  3. 收到文件弹通知、小图预览、最近列表

怎么用：
  双击「启动接收端.bat」
  - 收文件：看电脑面板二维码 / 等对方发过来
  - 发到另一台电脑：打开面板里的「电脑互传」
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import winsound
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import qrcode
from flask import Flask, jsonify, request, render_template_string, send_file, abort
from winotify import Notification, audio

# =========================
# 基本配置（一般不用改）
# =========================

PORT = 8765
# 打包成 exe 后，数据保存在 exe 同目录；开发时保存在脚本目录
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
# 默认保存目录；对方可在电脑面板里改成自己选的文件夹
SAVE_DIR = BASE_DIR / "received"
SAVE_DIR.mkdir(parents=True, exist_ok=True)

# 最近收到的文件记录（内存里，重启后从磁盘重新扫描）
RECENT_LIMIT = 30
_save_lock = threading.Lock()

app = Flask(__name__)


def _read_config() -> dict:
    """读取配置文件（保存目录、WiFi 等）。"""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_config(data: dict) -> None:
    """写入完整配置（合并已有字段）。"""
    cur = _read_config()
    cur.update(data)
    CONFIG_FILE.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_save_dir_from_config() -> None:
    """启动时读取对方上次选过的保存位置。"""
    global SAVE_DIR
    raw = str(_read_config().get("save_dir") or "").strip()
    if not raw:
        return
    try:
        path = Path(raw)
        path.mkdir(parents=True, exist_ok=True)
        SAVE_DIR = path.resolve()
        print(f"[配置] 保存目录：{SAVE_DIR}")
    except Exception as e:
        print(f"[配置] 读取保存目录失败：{e}")


def _persist_save_dir(path: Path) -> None:
    """把保存位置写到配置文件，下次启动还记得。"""
    _write_config({"save_dir": str(path)})


def get_wifi_config() -> dict:
    """读取已保存的 WiFi 预设（用于生成一键连网二维码）。"""
    cfg = _read_config()
    return {
        "ssid": str(cfg.get("wifi_ssid") or "").strip(),
        "password": str(cfg.get("wifi_password") or ""),
        "auth": str(cfg.get("wifi_auth") or "WPA").strip() or "WPA",
    }


def set_wifi_config(ssid: str, password: str, auth: str = "WPA") -> dict:
    """保存 WiFi 预设。"""
    auth = (auth or "WPA").upper()
    if auth not in ("WPA", "WEP", "nopass"):
        auth = "WPA"
    data = {
        "wifi_ssid": ssid.strip(),
        "wifi_password": password,
        "wifi_auth": auth,
    }
    _write_config(data)
    return get_wifi_config()


def escape_wifi_qr_field(value: str) -> str:
    """WIFI 二维码字段转义：\\ ; , \" :"""
    out = []
    for ch in value:
        if ch in ("\\", ";", ",", '"', ":"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def build_wifi_qr_payload(ssid: str, password: str, auth: str = "WPA") -> str:
    """
    生成手机可识别的 WiFi 二维码内容。
    格式：WIFI:T:WPA;S:名称;P:密码;;
    用系统相机扫码后可直接连 WiFi（iOS/Android 均支持）。
    """
    auth = (auth or "WPA").upper()
    if auth not in ("WPA", "WEP", "nopass"):
        auth = "WPA"
    s = escape_wifi_qr_field(ssid)
    if auth == "nopass":
        return f"WIFI:T:nopass;S:{s};;"
    p = escape_wifi_qr_field(password)
    return f"WIFI:T:{auth};S:{s};P:{p};;"


def detect_current_wifi_ssid() -> str:
    """尝试读取本机当前连接的 WiFi 名称（Windows netsh）。"""
    try:
        import subprocess

        r = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            # 中英文系统兼容
            if line.startswith("SSID") and "BSSID" not in line:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    return parts[1].strip()
            if "SSID" in line and "BSSID" not in line and ":" in line:
                # 例如：SSID                   : MyWifi
                left, right = line.split(":", 1)
                if left.strip() in ("SSID", "SSID 名称"):
                    return right.strip()
    except Exception:
        pass
    return ""


def set_save_dir(path: Path) -> Path:
    """切换保存目录（接收方自己选）。"""
    global SAVE_DIR
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    with _save_lock:
        SAVE_DIR = path
        _persist_save_dir(SAVE_DIR)
    return SAVE_DIR


def pick_save_folder_dialog() -> str:
    """弹出 Windows 文件夹选择框，让接收方自己选保存位置。"""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    initial = str(SAVE_DIR) if SAVE_DIR.exists() else str(BASE_DIR)
    chosen = filedialog.askdirectory(title="选择收到文件的保存位置", initialdir=initial)
    root.destroy()
    return chosen or ""


# 启动时加载自定义保存路径
_load_save_dir_from_config()


@app.after_request
def add_cors_headers(response):
    """允许手机端跨域访问。"""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def get_lan_ip() -> str:
    """获取本机局域网 IP（手机要连这个地址）。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def get_hostname() -> str:
    """本机电脑名称，用来区分两台电脑（避免看错成同一台）。"""
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def phone_url() -> str:
    """手机应打开的地址。"""
    return f"http://{get_lan_ip()}:{PORT}"


def _probe_peer(ip: str) -> dict | None:
    """探测某 IP 上是否运行着本程序的接收端。"""
    my_ip = get_lan_ip()
    if ip == my_ip:
        return None
    try:
        url = f"http://{ip}:{PORT}/ping"
        with urllib.request.urlopen(url, timeout=0.35) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        if data.get("ok"):
            return {
                "ip": ip,
                "url": f"http://{ip}:{PORT}",
                "save_dir": data.get("save_dir") or "",
                "hostname": data.get("hostname") or ip,
                "message": data.get("message") or "在线",
            }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def discover_peers() -> list[dict]:
    """
    扫描本机所在网段（例如 192.168.10.1~254），
    找出同样开着接收端（8765端口）的其他电脑。
    """
    my_ip = get_lan_ip()
    parts = my_ip.split(".")
    if len(parts) != 4:
        return []
    prefix = ".".join(parts[:3])
    candidates = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != my_ip]

    found: list[dict] = []
    # 并行探测，几十秒内扫完整个网段
    with ThreadPoolExecutor(max_workers=80) as pool:
        futures = [pool.submit(_probe_peer, ip) for ip in candidates]
        for fut in as_completed(futures):
            item = fut.result()
            if item:
                found.append(item)
    found.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    return found


def notify_received(file_names: list[str]) -> None:
    """文件到达时：弹 Windows 通知 + 叮一声。"""
    title = "收到局域网文件"
    if len(file_names) == 1:
        msg = file_names[0]
    else:
        msg = f"共 {len(file_names)} 个文件\n" + "\n".join(file_names[:5])

    try:
        toast = Notification(
            app_id="手机传到电脑",
            title=title,
            msg=msg,
            duration="short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.add_actions(label="打开文件夹", launch=str(SAVE_DIR))
        toast.show()
    except Exception as e:
        print(f"[通知失败] {e}")

    try:
        winsound.MessageBeep(winsound.MB_OK)
    except Exception:
        pass


def list_recent_files() -> list[dict]:
    """扫描 received 目录，返回最近文件信息（含预览类型）。"""
    image_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
    pdf_ext = {".pdf"}
    text_ext = {".txt", ".md", ".json", ".csv", ".log", ".xml", ".html", ".htm", ".css", ".js", ".py"}
    video_ext = {".mp4", ".webm", ".mov", ".mkv"}

    files = []
    for p in SAVE_DIR.iterdir():
        if p.is_file() and not p.name.startswith("."):
            st = p.stat()
            ext = p.suffix.lower()
            if ext in image_ext:
                kind = "image"
            elif ext in pdf_ext:
                kind = "pdf"
            elif ext in text_ext:
                kind = "text"
            elif ext in video_ext:
                kind = "video"
            else:
                kind = "other"
            files.append(
                {
                    "name": p.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "mtime_text": datetime.fromtimestamp(st.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "kind": kind,
                    "url": f"/file/{p.name}",
                }
            )
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files[:RECENT_LIMIT]


def human_size(n: int) -> str:
    """把字节数变成可读大小。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# =========================
# 手机端页面：上传文件
# =========================

PHONE_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
  <title>发送到电脑</title>
  <style>
    :root { --bg:#0f172a; --card:#1e293b; --text:#f8fafc; --muted:#94a3b8; --accent:#38bdf8; --ok:#4ade80; --bad:#f87171; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: "Segoe UI", system-ui, sans-serif; background: linear-gradient(160deg,#0f172a,#1e3a5f); color:var(--text); min-height:100vh; }
    .wrap { max-width:480px; margin:0 auto; padding:20px 16px 40px; }
    h1 { font-size:24px; margin:8px 0 4px; }
    .sub { color:var(--muted); font-size:14px; margin-bottom:20px; }
    .card { background:var(--card); border-radius:16px; padding:18px; margin-bottom:14px; box-shadow:0 8px 24px rgba(0,0,0,.25); }
    .drop {
      border:2px dashed #475569; border-radius:14px; padding:28px 16px; text-align:center; color:var(--muted);
    }
    .drop strong { display:block; color:var(--text); font-size:16px; margin-bottom:6px; }
    input[type=file] { display:none; }
    .btns { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:14px; }
    button, .btn {
      display:inline-flex; align-items:center; justify-content:center; gap:6px;
      width:100%; padding:14px 12px; border:0; border-radius:12px; font-size:15px; font-weight:600; cursor:pointer;
      text-decoration:none; color:#0f172a; background:var(--accent);
    }
    .btn.secondary { background:#334155; color:var(--text); }
    .btn.send { background:var(--ok); margin-top:10px; }
    .btn:disabled { opacity:.5; cursor:not-allowed; }
    .list-wrap { margin-top:14px; border-top:1px solid #334155; padding-top:10px; }
    .list-toggle {
      width:100%; display:flex; align-items:center; justify-content:space-between;
      background:transparent; color:var(--muted); font-size:13px; font-weight:600;
      padding:8px 4px; border:0; cursor:pointer;
    }
    .list-toggle span.arrow { transition: transform .2s; }
    .list-wrap.collapsed .list-toggle span.arrow { transform: rotate(-90deg); }
    .list-wrap.collapsed #fileList { display:none; }
    #fileList { margin-top:8px; display:flex; flex-direction:column; gap:8px; }
    .file-row {
      background:#0f172a; border-radius:12px; padding:10px 12px; display:grid;
      grid-template-columns:56px 1fr; gap:10px; align-items:start;
    }
    .file-row img { width:56px; height:56px; object-fit:cover; border-radius:8px; }
    .file-ico {
      width:56px; height:56px; border-radius:8px; background:#334155; display:flex;
      align-items:center; justify-content:center; font-size:11px; color:var(--muted); text-align:center; padding:4px;
    }
    .file-meta { min-width:0; overflow:hidden; }
    /* 文件名允许换行，不超出卡片 */
    .file-name {
      font-size:13px; line-height:1.4; word-break:break-all; overflow-wrap:anywhere;
      white-space:normal; max-width:100%;
    }
    .file-sub { font-size:12px; color:var(--muted); margin-top:4px; word-break:break-all; overflow-wrap:anywhere; }
    .file-sub.ok { color:var(--ok); }
    .file-sub.bad { color:var(--bad); }
    .file-sub.run { color:var(--accent); }
    .mini-bar { height:6px; background:#1e293b; border-radius:99px; overflow:hidden; margin-top:6px; }
    .mini-bar > i { display:block; height:100%; width:0; background:linear-gradient(90deg,#38bdf8,#4ade80); transition:width .15s; }
    .bar { height:8px; background:#0f172a; border-radius:99px; overflow:hidden; margin-top:12px; display:none; }
    .bar > i { display:block; height:100%; width:0; background:linear-gradient(90deg,#38bdf8,#4ade80); transition:width .2s; }
    #msg { margin-top:12px; font-size:14px; white-space:pre-wrap; word-break:break-all; min-height:20px; }
    #msg.ok { color:var(--ok); }
    #msg.bad { color:var(--bad); }
    .hint { font-size:12px; color:var(--muted); margin-top:8px; line-height:1.5; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>发送到电脑</h1>
    <p class="sub">多文件会异步并行发送，互不等待</p>

    <div class="card">
      <div class="drop" id="drop">
        <strong>点下面按钮选择文件</strong>
        支持多选 · 每个文件单独异步上传
      </div>
      <input id="file" type="file" multiple accept="*/*" />
      <input id="camera" type="file" accept="image/*" capture="environment" />
      <!-- 选文件 / 拍照 -->
      <div class="btns">
        <button type="button" class="btn secondary" id="btnPick">选文件</button>
        <button type="button" class="btn secondary" id="btnCam">拍一张</button>
      </div>
      <!-- 发送按钮紧挨在选文件下面 -->
      <button type="button" class="btn send" id="btnSend" disabled>发送到电脑</button>
      <div class="bar" id="bar"><i id="barInner"></i></div>
      <div id="msg"></div>

      <!-- 文件列表放在下方，默认收起，点标题展开 -->
      <div class="list-wrap collapsed" id="listWrap">
        <button type="button" class="list-toggle" id="btnToggleList">
          <span id="listTitle">已选文件（0）</span>
          <span class="arrow">▼</span>
        </button>
        <div id="fileList"></div>
      </div>

      <p class="hint">提示：多个文件会同时上传（最多 3 个并行），单个失败不影响其他文件。</p>
    </div>
  </div>

  <script>
    const fileInput = document.getElementById('file');
    const cameraInput = document.getElementById('camera');
    const fileList = document.getElementById('fileList');
    const listWrap = document.getElementById('listWrap');
    const listTitle = document.getElementById('listTitle');
    const btnSend = document.getElementById('btnSend');
    const msg = document.getElementById('msg');
    const bar = document.getElementById('bar');
    const barInner = document.getElementById('barInner');
    // 同时最多并行上传几个文件（太多会抢带宽）
    const MAX_PARALLEL = 3;
    let selected = [];
    let sending = false;

    document.getElementById('btnToggleList').onclick = () => {
      listWrap.classList.toggle('collapsed');
    };

    function fmtSize(n) {
      if (n < 1024) return n + ' B';
      if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
      return (n/1024/1024).toFixed(1) + ' MB';
    }

    function renderList() {
      fileList.innerHTML = '';
      listTitle.textContent = '已选文件（' + selected.length + '）';
      selected.forEach((item, idx) => {
        const row = document.createElement('div');
        row.className = 'file-row';
        row.id = 'row-' + idx;

        let left;
        if (item.file.type && item.file.type.startsWith('image/')) {
          left = document.createElement('img');
          left.src = URL.createObjectURL(item.file);
        } else {
          left = document.createElement('div');
          left.className = 'file-ico';
          left.textContent = 'FILE';
        }

        const meta = document.createElement('div');
        meta.className = 'file-meta';
        meta.innerHTML =
          '<div class="file-name"></div>' +
          '<div class="file-sub" id="sub-' + idx + '">待发送 · ' + fmtSize(item.file.size) + '</div>' +
          '<div class="mini-bar"><i id="p-' + idx + '"></i></div>';
        meta.querySelector('.file-name').textContent = item.file.name;

        row.appendChild(left);
        row.appendChild(meta);
        fileList.appendChild(row);
      });
      btnSend.disabled = selected.length === 0 || sending;
    }

    function setStatus(idx, text, cls, percent) {
      const sub = document.getElementById('sub-' + idx);
      const p = document.getElementById('p-' + idx);
      if (sub) { sub.textContent = text; sub.className = 'file-sub' + (cls ? ' ' + cls : ''); }
      if (p && typeof percent === 'number') p.style.width = Math.max(0, Math.min(100, percent)) + '%';
    }

    function takeFiles(list) {
      if (sending) return;
      selected = Array.from(list || []).map((file) => ({ file, status: 'pending' }));
      renderList();
      // 选完文件后展开列表，方便确认；发送按钮已在上方不用滚到底
      if (selected.length) listWrap.classList.remove('collapsed');
      else listWrap.classList.add('collapsed');
      msg.textContent = selected.length ? ('已选 ' + selected.length + ' 个文件，点上方「发送到电脑」') : '';
      msg.className = '';
      bar.style.display = 'none';
      barInner.style.width = '0%';
    }

    document.getElementById('btnPick').onclick = () => { if (!sending) fileInput.click(); };
    document.getElementById('btnCam').onclick = () => { if (!sending) cameraInput.click(); };
    fileInput.onchange = () => takeFiles(fileInput.files);
    cameraInput.onchange = () => takeFiles(cameraInput.files);

    /** 单个文件异步上传，返回 Promise */
    function uploadOne(item, idx) {
      return new Promise((resolve) => {
        const fd = new FormData();
        fd.append('file', item.file);
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/upload');
        setStatus(idx, '上传中 0% · ' + fmtSize(item.file.size), 'run', 0);

        xhr.upload.onprogress = (e) => {
          if (!e.lengthComputable) return;
          const pct = Math.round(e.loaded / e.total * 100);
          setStatus(idx, '上传中 ' + pct + '% · ' + fmtSize(item.file.size), 'run', pct);
          refreshOverall();
        };

        xhr.onload = () => {
          try {
            const data = JSON.parse(xhr.responseText);
            if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
              item.status = 'ok';
              item.saved = (data.saved && data.saved[0]) || item.file.name;
              setStatus(idx, '完成 · ' + item.saved, 'ok', 100);
              resolve({ ok: true, idx, saved: item.saved });
            } else {
              item.status = 'bad';
              const err = (data && data.error) || ('HTTP ' + xhr.status);
              setStatus(idx, '失败：' + err, 'bad', 0);
              resolve({ ok: false, idx, error: err });
            }
          } catch (e) {
            item.status = 'bad';
            setStatus(idx, '失败：返回数据异常', 'bad', 0);
            resolve({ ok: false, idx, error: 'bad json' });
          }
          refreshOverall();
        };

        xhr.onerror = () => {
          item.status = 'bad';
          setStatus(idx, '失败：网络错误', 'bad', 0);
          refreshOverall();
          resolve({ ok: false, idx, error: 'network' });
        };

        xhr.send(fd);
      });
    }

    /** 根据每个文件进度，更新总进度条 */
    function refreshOverall() {
      if (!selected.length) return;
      let sum = 0;
      selected.forEach((item, idx) => {
        const p = document.getElementById('p-' + idx);
        const w = p ? parseFloat(p.style.width) || 0 : 0;
        if (item.status === 'ok') sum += 100;
        else if (item.status === 'bad') sum += 100;
        else sum += w;
      });
      const pct = Math.round(sum / selected.length);
      barInner.style.width = pct + '%';
    }

    /**
     * 异步并发池：同时最多 MAX_PARALLEL 个上传，其余排队。
     * 每个文件独立请求，互不等待对方传完。
     */
    async function uploadAllAsync(items) {
      const results = new Array(items.length);
      let next = 0;

      async function worker() {
        while (next < items.length) {
          const i = next++;
          results[i] = await uploadOne(items[i], i);
        }
      }

      const workers = [];
      const n = Math.min(MAX_PARALLEL, items.length);
      for (let k = 0; k < n; k++) workers.push(worker());
      await Promise.all(workers);
      return results;
    }

    btnSend.onclick = async () => {
      if (!selected.length || sending) return;
      sending = true;
      btnSend.disabled = true;
      // 发送时展开列表，方便看每个文件进度
      listWrap.classList.remove('collapsed');
      msg.className = '';
      msg.textContent = '正在异步发送 ' + selected.length + ' 个文件（最多同时 ' + MAX_PARALLEL + ' 个）...';
      bar.style.display = 'block';
      barInner.style.width = '0%';
      selected.forEach((it) => { it.status = 'pending'; });

      const results = await uploadAllAsync(selected);
      const okList = results.filter((r) => r && r.ok);
      const badList = results.filter((r) => r && !r.ok);

      sending = false;
      btnSend.disabled = false;

      if (badList.length === 0) {
        msg.className = 'ok';
        msg.textContent = '全部发送成功（' + okList.length + ' 个）\\n' + okList.map((r) => r.saved).join('\\n');
        selected = [];
        fileInput.value = '';
        cameraInput.value = '';
        // 保留列表一眼看结果，稍后再清也可以；这里保留完成状态
      } else if (okList.length === 0) {
        msg.className = 'bad';
        msg.textContent = '全部失败，请检查 WiFi / 接收端是否在运行';
      } else {
        msg.className = 'bad';
        msg.textContent = '部分成功：成功 ' + okList.length + ' 个，失败 ' + badList.length + ' 个';
      }
      refreshOverall();
    };
  </script>
</body>
</html>
"""


# =========================
# 电脑端面板：二维码 + 最近文件 + 预览
# =========================

PC_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SYNC · 手机传到电脑</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Share+Tech+Mono&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg0:#050a12;
      --bg1:#0a1628;
      --card:rgba(8, 20, 40, .82);
      --text:#e8f7ff;
      --muted:#7aa0b8;
      --line:rgba(0, 229, 255, .22);
      --accent:#00e5ff;
      --accent2:#39ff14;
      --warn:#ffb020;
      --ok:#39ff14;
      --glow:0 0 18px rgba(0,229,255,.35);
    }
    * { box-sizing:border-box; }
    body {
      margin:0; color:var(--text);
      font-family:"Share Tech Mono", Consolas, monospace;
      background: var(--bg0);
      min-height:100vh;
      overflow-x:hidden;
    }
    /* 科技感背景：网格 + 光晕 + 扫描线 */
    body::before {
      content:""; position:fixed; inset:0; z-index:0; pointer-events:none;
      background:
        radial-gradient(ellipse 80% 50% at 20% -10%, rgba(0,229,255,.18), transparent 55%),
        radial-gradient(ellipse 60% 40% at 90% 10%, rgba(57,255,20,.08), transparent 50%),
        radial-gradient(ellipse 50% 40% at 50% 100%, rgba(0,120,255,.12), transparent 55%),
        linear-gradient(180deg, var(--bg0), var(--bg1) 40%, #06101c);
    }
    body::after {
      content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.35;
      background-image:
        linear-gradient(rgba(0,229,255,.06) 1px, transparent 1px),
        linear-gradient(90deg, rgba(0,229,255,.06) 1px, transparent 1px);
      background-size:48px 48px;
      mask-image: radial-gradient(ellipse at center, black 30%, transparent 80%);
    }
    .scan {
      position:fixed; left:0; right:0; height:120px; z-index:1; pointer-events:none;
      background: linear-gradient(180deg, transparent, rgba(0,229,255,.06), transparent);
      animation: scanMove 5.5s linear infinite;
    }
    @keyframes scanMove {
      0% { top:-120px; }
      100% { top:110%; }
    }
    .wrap { position:relative; z-index:2; max-width:1100px; margin:0 auto; padding:28px 20px 48px; }

    .brand {
      display:flex; align-items:flex-end; justify-content:space-between; gap:16px; flex-wrap:wrap;
      margin-bottom:8px;
    }
    h1 {
      margin:0; font-family:Orbitron, sans-serif; font-weight:700; font-size:28px; letter-spacing:.08em;
      text-transform:uppercase;
      background: linear-gradient(90deg, #fff, var(--accent) 50%, var(--accent2));
      -webkit-background-clip:text; background-clip:text; color:transparent;
      text-shadow: 0 0 30px rgba(0,229,255,.25);
      animation: titlePulse 3.2s ease-in-out infinite;
    }
    @keyframes titlePulse {
      0%,100% { filter:brightness(1); }
      50% { filter:brightness(1.15); }
    }
    .hud-tag {
      font-size:11px; letter-spacing:.18em; color:var(--accent);
      border:1px solid var(--line); padding:6px 10px; border-radius:4px;
      box-shadow: inset 0 0 12px rgba(0,229,255,.08);
    }
    .sub { color:var(--muted); margin:0 0 22px; font-size:13px; letter-spacing:.04em; }

    .grid { display:grid; grid-template-columns: 300px 1fr; gap:18px; }
    @media (max-width:900px) { .grid { grid-template-columns:1fr; } }

    .card {
      position:relative; background:var(--card); border-radius:6px; padding:20px;
      border:1px solid var(--line);
      box-shadow: var(--glow), inset 0 0 40px rgba(0,229,255,.04);
      backdrop-filter: blur(10px);
      overflow:hidden;
    }
    /* 四角装饰 */
    .card::before, .card::after {
      content:""; position:absolute; width:14px; height:14px; pointer-events:none;
      border:2px solid var(--accent);
    }
    .card::before { top:6px; left:6px; border-right:0; border-bottom:0; }
    .card::after { bottom:6px; right:6px; border-left:0; border-top:0; }
    .card .corner-tr, .card .corner-bl {
      position:absolute; width:14px; height:14px; pointer-events:none;
      border:2px solid var(--accent); opacity:.7;
    }
    .card .corner-tr { top:6px; right:6px; border-left:0; border-bottom:0; }
    .card .corner-bl { bottom:6px; left:6px; border-right:0; border-top:0; }

    .qr {
      width:200px; height:200px; display:block; margin:14px auto; border-radius:4px; background:#fff;
      padding:8px; box-shadow: 0 0 0 1px var(--accent), 0 0 24px rgba(0,229,255,.35);
      animation: qrGlow 2.8s ease-in-out infinite;
    }
    @keyframes qrGlow {
      0%,100% { box-shadow: 0 0 0 1px var(--accent), 0 0 16px rgba(0,229,255,.25); }
      50% { box-shadow: 0 0 0 1px #fff, 0 0 28px rgba(0,229,255,.55); }
    }
    .url {
      font-family:"Share Tech Mono", monospace; background:rgba(0,0,0,.35);
      border:1px solid var(--line); border-radius:4px; padding:10px 12px;
      word-break:break-all; font-size:12px; color:var(--accent);
      box-shadow: inset 0 0 16px rgba(0,229,255,.06);
    }
    .ip-hero {
      margin:12px 0 4px; padding:12px; border:1px solid rgba(57,255,20,.35);
      border-radius:4px; background:rgba(0,40,20,.25); text-align:center;
      box-shadow:0 0 16px rgba(57,255,20,.15);
    }
    .ip-label { font-size:11px; color:var(--muted); letter-spacing:.08em; margin-bottom:6px; }
    .ip-big {
      font-family:Orbitron, sans-serif; font-size:28px; letter-spacing:.06em;
      color:var(--ok); text-shadow:0 0 16px rgba(57,255,20,.45); word-break:break-all;
    }
    .host-name {
      font-family:Orbitron, sans-serif; font-size:16px; color:#fff; letter-spacing:.04em;
      word-break:break-all;
    }
    .warn-same {
      margin-top:12px; padding:10px; font-size:12px; line-height:1.6; color:#ffb020;
      border:1px dashed rgba(255,176,32,.45); border-radius:4px; text-align:left;
      background:rgba(80,40,0,.25);
    }
    .save-box {
      margin:10px 0 0; padding:10px 12px; border:1px solid var(--line); border-radius:4px;
      background:rgba(0,0,0,.28);
    }
    .save-path {
      margin-top:6px; font-size:12px; color:var(--accent); word-break:break-all; line-height:1.5;
    }
    .row { display:flex; gap:8px; margin-top:12px; flex-wrap:wrap; }
    button {
      border:1px solid rgba(0,229,255,.45); border-radius:4px; padding:10px 14px;
      font-size:12px; font-weight:700; cursor:pointer; letter-spacing:.06em;
      font-family:Orbitron, sans-serif; text-transform:uppercase;
      background: linear-gradient(180deg, rgba(0,229,255,.35), rgba(0,120,180,.25));
      color:var(--text); box-shadow: 0 0 12px rgba(0,229,255,.2);
      transition: transform .15s, box-shadow .15s, border-color .15s;
    }
    button:hover:not(:disabled) {
      transform: translateY(-1px);
      border-color:var(--accent);
      box-shadow: 0 0 20px rgba(0,229,255,.45);
    }
    button:disabled { opacity:.35; cursor:not-allowed; }
    button.ghost {
      background: rgba(0,20,40,.5);
      border-color: rgba(122,160,184,.35);
      color:var(--muted);
      box-shadow:none;
    }
    button.ghost:hover:not(:disabled) { color:var(--text); border-color:var(--accent); }

    .status {
      display:inline-flex; align-items:center; gap:8px; color:var(--ok);
      font-weight:700; font-size:12px; letter-spacing:.14em; text-transform:uppercase;
    }
    .dot {
      width:8px; height:8px; border-radius:50%; background:var(--ok);
      box-shadow:0 0 0 4px rgba(57,255,20,.15), 0 0 12px var(--ok);
      animation: blink 1.4s ease-in-out infinite;
    }
    @keyframes blink {
      0%,100% { opacity:1; }
      50% { opacity:.45; }
    }
    .empty { color:var(--muted); padding:24px 8px; text-align:center; letter-spacing:.08em; }
    .tip { margin-top:14px; color:var(--muted); font-size:12px; line-height:1.7; }
    .tip b { color:var(--accent); font-weight:400; }

    #toast {
      position:fixed; right:20px; bottom:20px; z-index:50;
      background:rgba(0,20,40,.92); color:var(--accent); padding:12px 16px;
      border:1px solid var(--line); border-radius:4px; display:none; max-width:320px;
      box-shadow: var(--glow); letter-spacing:.06em; font-size:13px;
    }

    .files { display:flex; flex-direction:column; gap:10px; max-height:70vh; overflow:auto; }
    .files::-webkit-scrollbar { width:6px; }
    .files::-webkit-scrollbar-thumb { background:rgba(0,229,255,.35); border-radius:4px; }

    .item {
      display:grid; grid-template-columns:88px 1fr auto; gap:12px; align-items:center;
      padding:10px; border:1px solid rgba(0,229,255,.15); border-radius:4px;
      background:rgba(0,10,24,.55); cursor:pointer;
      transition: border-color .15s, box-shadow .15s, background .15s;
    }
    .item:hover, .item.active {
      border-color:var(--accent);
      background:rgba(0,40,60,.45);
      box-shadow: 0 0 0 1px rgba(0,229,255,.2), 0 0 20px rgba(0,229,255,.15);
    }
    .thumb {
      width:88px; height:88px; border-radius:4px; object-fit:cover; background:#021018; display:block;
      border:1px solid rgba(0,229,255,.25);
    }
    .thumb-ph {
      width:88px; height:88px; border-radius:4px; background:#021018; display:flex; align-items:center;
      justify-content:center; color:var(--accent); font-size:11px; font-weight:700; text-align:center;
      padding:6px; border:1px dashed rgba(0,229,255,.35); letter-spacing:.1em;
      font-family:Orbitron, sans-serif;
    }
    .iname { font-size:13px; font-weight:600; word-break:break-all; color:#dff6ff; }
    .imeta { color:var(--muted); font-size:11px; margin-top:4px; letter-spacing:.04em; }
    .itag {
      display:inline-block; margin-top:6px; font-size:10px; padding:2px 8px; border-radius:2px;
      background:rgba(0,229,255,.12); color:var(--accent); border:1px solid rgba(0,229,255,.3);
      letter-spacing:.12em; text-transform:uppercase;
    }

    .preview-box { display:flex; flex-direction:column; }
    .preview-title { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:12px; }
    .preview-title strong {
      font-size:13px; word-break:break-all; font-family:Orbitron, sans-serif;
      letter-spacing:.06em; color:var(--accent);
    }
    .preview-stage {
      width:220px; height:220px; margin:0 auto; background:rgba(0,0,0,.45);
      border:1px solid var(--line); border-radius:4px;
      display:flex; align-items:center; justify-content:center; overflow:hidden;
      position:relative; box-shadow: inset 0 0 30px rgba(0,229,255,.08), var(--glow);
    }
    .preview-stage::before {
      content:"PREVIEW"; position:absolute; top:8px; left:10px; font-size:9px;
      letter-spacing:.2em; color:rgba(0,229,255,.45); font-family:Orbitron, sans-serif; pointer-events:none;
    }
    .preview-stage img, .preview-stage video {
      max-width:200px; max-height:200px; width:auto; height:auto; object-fit:contain; display:block;
      border-radius:2px;
    }
    .preview-stage iframe { width:200px; height:200px; border:0; background:#fff; border-radius:2px; }
    .preview-stage pre {
      margin:0; padding:10px; color:#9fefff; font-size:11px; white-space:pre-wrap; word-break:break-word;
      width:100%; height:100%; overflow:auto; font-family:"Share Tech Mono", monospace;
    }
    .preview-empty { color:var(--muted); text-align:center; padding:16px; font-size:12px; line-height:1.6; }
    .preview-actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; justify-content:center; }

    .section-head {
      display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;
      font-family:Orbitron, sans-serif; letter-spacing:.1em; font-size:13px; color:var(--accent);
    }
    .section-head span { color:var(--muted); font-family:"Share Tech Mono", monospace; font-size:11px; letter-spacing:.08em; }

    .lightbox {
      display:none; position:fixed; inset:0; background:rgba(2,8,16,.92); z-index:100;
      align-items:center; justify-content:center; padding:24px;
    }
    .lightbox.show { display:flex; }
    .lightbox img, .lightbox video { max-width:96vw; max-height:90vh; object-fit:contain; border-radius:4px;
      box-shadow:0 0 40px rgba(0,229,255,.35); }
    .lightbox .close {
      position:absolute; top:16px; right:16px;
    }
    .edit-toggle {
      width:100%; margin-top:14px; display:flex; align-items:center; justify-content:space-between;
      padding:10px 12px; border:1px solid var(--line); border-radius:4px; cursor:pointer;
      background:rgba(0,20,40,.55); color:var(--accent); font-family:Orbitron, sans-serif;
      font-size:12px; letter-spacing:.1em; text-transform:uppercase;
    }
    .edit-toggle .arrow { transition: transform .2s; color:var(--muted); }
    .edit-panel { display:none; margin-top:10px; padding-top:4px; border-top:1px dashed rgba(0,229,255,.2); }
    .edit-panel.open { display:block; }
    .edit-toggle.open .arrow { transform: rotate(180deg); }
  </style>
</head>
<body>
  <div class="scan"></div>
  <div class="wrap">
    <div class="brand">
      <h1>Phone → PC Sync</h1>
      <div class="hud-tag">LAN LINK // ACTIVE</div>
    </div>
    <p class="sub">SECURE CHANNEL · SAME WIFI · AUTO PREVIEW ·
      <a href="/send" style="color:#00e5ff;text-decoration:none;border-bottom:1px solid rgba(0,229,255,.4);">电脑互传 →</a>
    </p>

    <div class="grid">
      <div class="card">
        <span class="corner-tr"></span><span class="corner-bl"></span>
        <div class="status"><span class="dot"></span>接收端在线 · <span id="myHost" style="color:var(--muted);letter-spacing:.04em;">{{ hostname }}</span></div>

        <img class="qr" id="qr" src="/qr.png" alt="上传二维码" />
        <div class="ip-label" style="text-align:center;margin-top:-4px;">上传页二维码</div>
        <div class="url" id="url">{{ url }}</div>
        <div class="ip-label" style="text-align:center;margin-top:8px;">本机 IP · <span id="myIp" style="color:var(--ok);">{{ ip }}</span></div>

        <div id="wifiQrWrap" style="{{ 'display:none;' if not wifi_ssid else '' }}text-align:center;margin-top:12px;">
          <img class="qr" id="wifiQr" src="/wifi-qr.png?t={{ wifi_ssid }}" alt="WiFi二维码"
            style="width:160px;height:160px;" />
          <div class="ip-label">连 WiFi 二维码（系统相机扫）</div>
        </div>

        <p class="tip" style="margin-top:12px;">
          <b>01</b> 同一 WiFi（可先扫连网码）<br/>
          <b>02</b> 再扫上传码传文件
        </p>

        <!-- 不常用：收进可折叠编辑栏 -->
        <button type="button" class="edit-toggle" id="btnEditToggle">
          <span>编辑设置</span>
          <span class="arrow">▼</span>
        </button>
        <div class="edit-panel" id="editPanel">
          <div class="warn-same" id="warnSame" style="margin-top:0;">
            若两台电脑看到的 IP / 电脑名都一样：说明两边打开的是同一台的网页。<br/>
            对方请在<strong>自己电脑</strong>打开：http://127.0.0.1:8765/pc
          </div>

          <div class="save-box" style="margin-top:12px;">
            <div class="ip-label">WiFi 一键连接（可选）</div>
            <p style="margin:8px 0;color:var(--muted);font-size:12px;line-height:1.6;">
              填名称和密码并保存后，上方会显示「连 WiFi」码，用系统相机扫即可连网。
            </p>
            <label style="font-size:11px;color:var(--muted);">WiFi 名称 SSID</label>
            <input id="wifiSsid" type="text" value="{{ wifi_ssid }}" placeholder="例如 TP-LINK_XXXX"
              style="width:100%;margin:4px 0 8px;padding:8px;border-radius:4px;border:1px solid var(--line);background:rgba(0,0,0,.35);color:var(--accent);font-family:inherit;" />
            <label style="font-size:11px;color:var(--muted);">WiFi 密码</label>
            <input id="wifiPwd" type="password" value="{{ wifi_password }}" placeholder="输入密码"
              style="width:100%;margin:4px 0 8px;padding:8px;border-radius:4px;border:1px solid var(--line);background:rgba(0,0,0,.35);color:var(--accent);font-family:inherit;" />
            <div class="row" style="margin-top:0;">
              <button type="button" id="btnSaveWifi">保存并生成连网码</button>
              <button type="button" class="ghost" id="btnDetectWifi">读取当前 WiFi 名</button>
            </div>
          </div>

          <div class="save-box" style="margin-top:12px;">
            <div class="ip-label">收到的文件保存到</div>
            <div class="save-path" id="savePath">{{ save_dir }}</div>
            <div class="warn-same" id="remoteWarn" style="display:none;margin-top:10px;">
              你现在打开的是<strong>别人电脑</strong>上的接收端页面，所以「选择保存位置」会弹到对方屏幕上。<br/>
              请在自己电脑运行接收端，并打开：<strong>http://127.0.0.1:8765/pc</strong> 再选文件夹。
            </div>
          </div>

          <div class="row">
            <button type="button" id="btnCopyIp">复制本机 IP</button>
            <button type="button" id="btnCopy">复制完整链接</button>
            <button type="button" id="btnChooseSave">选择保存位置</button>
            <button type="button" class="ghost" id="btnOpenFolder">打开文件夹</button>
            <button type="button" class="ghost" id="btnRefreshQr">刷新上传码</button>
            <button type="button" class="ghost" id="btnToSend" onclick="location.href='/send'">电脑互传</button>
          </div>
        </div>
      </div>

      <div class="card preview-box">
        <span class="corner-tr"></span><span class="corner-bl"></span>
        <div class="preview-title">
          <strong id="previewName">FILE PREVIEW</strong>
          <button type="button" class="ghost" id="btnRefresh">刷新</button>
        </div>
        <div class="preview-stage" id="previewStage">
          <div class="preview-empty">等待数据流…<br/>手机发送后将在此显示小图</div>
        </div>
        <div class="preview-actions">
          <button type="button" class="ghost" id="btnOpenFile" disabled>系统打开</button>
          <button type="button" class="ghost" id="btnZoom" disabled>放大查看</button>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:18px;">
      <span class="corner-tr"></span><span class="corner-bl"></span>
      <div class="section-head">
        <strong>INCOMING FILES</strong>
        <span>CLICK TO PREVIEW</span>
      </div>
      <div id="list" class="files"><div class="empty">NO SIGNAL · 还没有文件</div></div>
    </div>
  </div>

  <div id="toast"></div>
  <div class="lightbox" id="lightbox">
    <button class="close" type="button" id="btnCloseLb">关闭</button>
    <div id="lightboxBody"></div>
  </div>

  <script>
    const url = {{ url|tojson }};
    const myIp = {{ ip|tojson }};
    const myHost = {{ hostname|tojson }};
    let lastNewest = '';
    let current = null;
    let filesCache = [];

    const kindLabel = { image:'图片', pdf:'PDF', text:'文本', video:'视频', other:'文件' };

    document.getElementById('btnEditToggle').onclick = () => {
      const btn = document.getElementById('btnEditToggle');
      const panel = document.getElementById('editPanel');
      const open = panel.classList.toggle('open');
      btn.classList.toggle('open', open);
    };

    function showToast(text) {
      const el = document.getElementById('toast');
      el.textContent = text;
      el.style.display = 'block';
      clearTimeout(showToast._t);
      showToast._t = setTimeout(() => el.style.display = 'none', 2500);
    }

    function human(n) {
      const u = ['B','KB','MB','GB'];
      let i = 0, x = n;
      while (x >= 1024 && i < u.length-1) { x /= 1024; i++; }
      return (i===0? x : x.toFixed(1)) + ' ' + u[i];
    }

    function esc(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }

    function thumbHtml(f) {
      if (f.kind === 'image') {
        return '<img class="thumb" src="' + esc(f.url) + '" alt="" loading="lazy" />';
      }
      const map = { pdf:'PDF', text:'TXT', video:'VIDEO', other:'FILE' };
      return '<div class="thumb-ph">' + (map[f.kind] || 'FILE') + '</div>';
    }

    async function showPreview(f, autoZoom) {
      current = f;
      document.getElementById('previewName').textContent = f.name;
      document.getElementById('btnOpenFile').disabled = false;
      document.getElementById('btnZoom').disabled = !(f.kind === 'image' || f.kind === 'video');

      document.querySelectorAll('.item').forEach((el) => {
        el.classList.toggle('active', el.dataset.name === f.name);
      });

      const stage = document.getElementById('previewStage');
      if (f.kind === 'image') {
        stage.innerHTML = '<img src="' + esc(f.url) + '" alt="' + esc(f.name) + '" />';
      } else if (f.kind === 'video') {
        stage.innerHTML = '<video src="' + esc(f.url) + '" controls muted></video>';
      } else if (f.kind === 'pdf') {
        stage.innerHTML = '<iframe src="' + esc(f.url) + '#toolbar=0" title="pdf"></iframe>';
      } else if (f.kind === 'text') {
        stage.innerHTML = '<div class="preview-empty">加载文本中...</div>';
        try {
          const t = await fetch(f.url).then((r) => r.text());
          const pre = document.createElement('pre');
          pre.textContent = t.slice(0, 20000);
          stage.innerHTML = '';
          stage.appendChild(pre);
        } catch (e) {
          stage.innerHTML = '<div class="preview-empty">文本读取失败</div>';
        }
      } else {
        stage.innerHTML =
          '<div class="preview-empty">暂无内嵌预览<br/>可点「系统打开」<br/><br/>' +
          esc(f.name) + '</div>';
      }
    }

    function openLightbox(f) {
      const body = document.getElementById('lightboxBody');
      if (f.kind === 'image') {
        body.innerHTML = '<img src="' + esc(f.url) + '" alt="" />';
      } else if (f.kind === 'video') {
        body.innerHTML = '<video src="' + esc(f.url) + '" controls autoplay></video>';
      } else {
        return;
      }
      document.getElementById('lightbox').classList.add('show');
    }

    function renderList(files) {
      const box = document.getElementById('list');
      if (!files.length) {
        box.innerHTML = '<div class="empty">NO SIGNAL · 还没有文件</div>';
        return;
      }
      box.innerHTML = files.map((f) => {
        return (
          '<div class="item" data-name="' + esc(f.name) + '">' +
            thumbHtml(f) +
            '<div>' +
              '<div class="iname">' + esc(f.name) + '</div>' +
              '<div class="imeta">' + human(f.size) + ' · ' + esc(f.mtime_text) + '</div>' +
              '<span class="itag">' + (kindLabel[f.kind] || '文件') + '</span>' +
            '</div>' +
            '<button type="button" class="ghost btn-prev">预览</button>' +
          '</div>'
        );
      }).join('');

      box.querySelectorAll('.item').forEach((el, idx) => {
        el.addEventListener('click', () => showPreview(files[idx], false));
      });
    }

    async function loadFiles() {
      const res = await fetch('/api/files');
      const data = await res.json();
      filesCache = data.files || [];
      renderList(filesCache);

      const newest = filesCache[0] ? filesCache[0].name : '';
      if (newest && lastNewest && newest !== lastNewest) {
        showToast('收到新文件，已显示小图预览');
        showPreview(filesCache[0], false);
      } else if (newest && !lastNewest) {
        showPreview(filesCache[0], false);
      } else if (current) {
        const still = filesCache.find((x) => x.name === current.name);
        if (still) {
          document.querySelectorAll('.item').forEach((el) => {
            el.classList.toggle('active', el.dataset.name === still.name);
          });
        }
      }
      lastNewest = newest;
    }

    document.getElementById('btnCopyIp').onclick = async () => {
      try {
        await navigator.clipboard.writeText(myIp);
        showToast('本机 IP 已复制：' + myIp);
      } catch (e) {
        prompt('请手动复制本机 IP：', myIp);
      }
    };
    document.getElementById('btnCopy').onclick = async () => {
      try {
        await navigator.clipboard.writeText(url);
        showToast('链接已复制');
      } catch (e) {
        prompt('请手动复制：', url);
      }
    };
    document.getElementById('btnOpenFolder').onclick = () => fetch('/api/open-folder', {method:'POST'});

    // 是否在「本机」打开页面：只有本机才能弹文件夹选择框
    const isLocalPage = ['127.0.0.1', 'localhost'].indexOf(location.hostname) >= 0;
    if (!isLocalPage) {
      const w = document.getElementById('remoteWarn');
      if (w) w.style.display = 'block';
      const btn = document.getElementById('btnChooseSave');
      btn.disabled = true;
      btn.title = '请在本机打开 http://127.0.0.1:8765/pc 再选择保存位置';
    }

    document.getElementById('btnChooseSave').onclick = async () => {
      if (!isLocalPage) {
        showToast('请在本机打开 http://127.0.0.1:8765/pc 再选文件夹');
        return;
      }
      showToast('请在弹出的窗口里选择文件夹…');
      try {
        const res = await fetch('/api/choose-save-dir', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          document.getElementById('savePath').textContent = data.save_dir;
          showToast('保存位置已更新');
        } else if (data.cancelled) {
          showToast('已取消选择');
        } else {
          showToast(data.error || '选择失败');
        }
      } catch (e) {
        showToast('选择失败，请重试');
      }
    };
    document.getElementById('btnRefresh').onclick = loadFiles;
    document.getElementById('btnRefreshQr').onclick = () => {
      document.getElementById('qr').src = '/qr.png?t=' + Date.now();
      fetch('/api/info').then(r=>r.json()).then(d => {
        document.getElementById('url').textContent = d.url;
        if (d.ip) document.getElementById('myIp').textContent = d.ip;
        if (d.hostname) document.getElementById('myHost').textContent = d.hostname;
        if (d.save_dir) document.getElementById('savePath').textContent = d.save_dir;
      });
      showToast('上传码已刷新');
    };

    document.getElementById('btnSaveWifi').onclick = async () => {
      const ssid = document.getElementById('wifiSsid').value.trim();
      const password = document.getElementById('wifiPwd').value;
      if (!ssid) { showToast('请先填写 WiFi 名称'); return; }
      try {
        const res = await fetch('/api/wifi-config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ ssid, password, auth: 'WPA' })
        });
        const data = await res.json();
        if (data.ok) {
          document.getElementById('wifiQrWrap').style.display = 'block';
          document.getElementById('wifiQr').src = '/wifi-qr.png?t=' + Date.now();
          showToast('连网二维码已生成，用手机系统相机扫');
        } else {
          showToast(data.error || '保存失败');
        }
      } catch (e) {
        showToast('保存失败');
      }
    };

    document.getElementById('btnDetectWifi').onclick = async () => {
      try {
        const res = await fetch('/api/wifi-detect');
        const data = await res.json();
        if (data.ok && data.ssid) {
          document.getElementById('wifiSsid').value = data.ssid;
          showToast('已填入当前 WiFi：' + data.ssid + '（密码需手动输入）');
        } else {
          showToast(data.error || '未能读取 WiFi 名，请手动填写');
        }
      } catch (e) {
        showToast('读取失败');
      }
    };
    document.getElementById('btnOpenFile').onclick = () => {
      if (!current) return;
      fetch('/api/open-file', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name: current.name })
      });
    };
    document.getElementById('btnZoom').onclick = () => {
      if (current) openLightbox(current);
    };
    document.getElementById('btnCloseLb').onclick = () => {
      document.getElementById('lightbox').classList.remove('show');
      document.getElementById('lightboxBody').innerHTML = '';
    };
    document.getElementById('lightbox').addEventListener('click', (e) => {
      if (e.target.id === 'lightbox') document.getElementById('btnCloseLb').click();
    });

    loadFiles();
    setInterval(loadFiles, 2000);
  </script>
</body>
</html>
"""


# =========================
# 电脑互传：发到另一台电脑
# =========================

SEND_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>电脑互传 · 同 WiFi 快速传文件</title>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Share+Tech+Mono&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg0:#050a12; --text:#e8f7ff; --muted:#7aa0b8; --line:rgba(0,229,255,.22);
      --accent:#00e5ff; --ok:#39ff14; --bad:#ff6b6b; --card:rgba(8,20,40,.88);
    }
    * { box-sizing:border-box; }
    body {
      margin:0; min-height:100vh; color:var(--text);
      font-family:"Share Tech Mono", Consolas, monospace;
      background:
        radial-gradient(ellipse 70% 40% at 10% 0%, rgba(0,229,255,.16), transparent 50%),
        radial-gradient(ellipse 50% 40% at 90% 100%, rgba(57,255,20,.08), transparent 50%),
        linear-gradient(180deg, #050a12, #0a1628);
    }
    .wrap { max-width:720px; margin:0 auto; padding:28px 18px 48px; }
    h1 {
      margin:0; font-family:Orbitron, sans-serif; font-size:24px; letter-spacing:.08em;
      background:linear-gradient(90deg,#fff,#00e5ff); -webkit-background-clip:text; background-clip:text; color:transparent;
    }
    .nav { margin:8px 0 18px; font-size:12px; }
    .nav a { color:var(--accent); text-decoration:none; border-bottom:1px solid rgba(0,229,255,.35); }
    .card {
      background:var(--card); border:1px solid var(--line); border-radius:6px; padding:18px;
      box-shadow:0 0 18px rgba(0,229,255,.2); margin-bottom:14px;
    }
    label { display:block; font-size:12px; color:var(--muted); letter-spacing:.1em; margin-bottom:8px; }
    .ip-row { display:flex; gap:8px; flex-wrap:wrap; }
    input[type=text] {
      flex:1; min-width:200px; padding:12px 14px; border-radius:4px; border:1px solid var(--line);
      background:rgba(0,0,0,.4); color:var(--accent); font-family:inherit; font-size:14px;
    }
    button {
      border:1px solid rgba(0,229,255,.45); border-radius:4px; padding:12px 16px; cursor:pointer;
      font-family:Orbitron, sans-serif; font-size:11px; letter-spacing:.06em; text-transform:uppercase;
      background:linear-gradient(180deg, rgba(0,229,255,.35), rgba(0,120,180,.25)); color:var(--text);
    }
    button.ghost { background:rgba(0,20,40,.5); color:var(--muted); }
    button:disabled { opacity:.4; cursor:not-allowed; }
    button.send { background:linear-gradient(180deg, rgba(57,255,20,.35), rgba(20,120,40,.25)); border-color:rgba(57,255,20,.5); width:100%; margin-top:12px; padding:14px; font-size:13px; }
    .drop {
      margin-top:12px; border:2px dashed rgba(0,229,255,.35); border-radius:6px; padding:28px 16px;
      text-align:center; color:var(--muted); cursor:pointer; transition:border-color .15s, background .15s;
    }
    .drop.drag { border-color:var(--accent); background:rgba(0,229,255,.08); color:var(--text); }
    .drop strong { display:block; color:var(--text); margin-bottom:6px; font-size:15px; }
    input[type=file] { display:none; }
    .status { margin-top:10px; font-size:13px; min-height:20px; }
    .status.ok { color:var(--ok); }
    .status.bad { color:var(--bad); }
    .status.run { color:var(--accent); }
    .hint { color:var(--muted); font-size:12px; line-height:1.7; margin-top:10px; }
    .file-list { margin-top:12px; display:flex; flex-direction:column; gap:8px; max-height:280px; overflow:auto; }
    .file-row {
      display:grid; grid-template-columns:1fr auto; gap:8px; align-items:start;
      background:rgba(0,0,0,.35); border:1px solid rgba(0,229,255,.15); border-radius:4px; padding:10px 12px;
    }
    .fname { font-size:13px; word-break:break-all; overflow-wrap:anywhere; }
    .fsub { font-size:11px; color:var(--muted); margin-top:4px; word-break:break-all; }
    .fsub.ok { color:var(--ok); } .fsub.bad { color:var(--bad); } .fsub.run { color:var(--accent); }
    .mini { height:5px; background:#123; border-radius:99px; margin-top:6px; overflow:hidden; }
    .mini > i { display:block; height:100%; width:0; background:linear-gradient(90deg,#00e5ff,#39ff14); }
    .bar { height:8px; background:#123; border-radius:99px; margin-top:12px; display:none; overflow:hidden; }
    .bar > i { display:block; height:100%; width:0; background:linear-gradient(90deg,#00e5ff,#39ff14); }
    .me { font-size:12px; color:var(--accent); margin-top:8px; word-break:break-all; }
    .peer {
      margin-top:8px; padding:10px 12px; border:1px solid rgba(57,255,20,.35); border-radius:4px;
      background:rgba(0,40,20,.2); cursor:pointer; display:flex; justify-content:space-between; gap:10px; align-items:center;
    }
    .peer:hover { border-color:var(--ok); box-shadow:0 0 12px rgba(57,255,20,.2); }
    .peer b { color:var(--ok); font-size:18px; font-family:Orbitron, sans-serif; letter-spacing:.04em; }
    .peer span { color:var(--muted); font-size:11px; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>PC ↔ PC TRANSFER</h1>
    <div class="nav"><a href="/pc">← 返回接收面板</a></div>

    <div class="card">
      <label>对方电脑 IP</label>
      <div class="ip-row">
        <input id="targetIp" type="text" placeholder="可手动填写，或点下方扫描自动出现" />
        <button type="button" class="ghost" id="btnTest">检测在线</button>
        <button type="button" id="btnScan">扫描对方电脑</button>
      </div>
      <div class="me">
        <div>当前这台电脑名：<b>{{ hostname }}</b></div>
        <div style="margin-top:6px;">本机 IP：<b id="myIpShow">{{ my_ip }}</b>
          <button type="button" class="ghost" id="btnCopyMyIp" style="padding:6px 10px;margin-left:8px;">复制本机 IP</button>
        </div>
        <div style="margin-top:6px;">完整地址：{{ my_url }}</div>
        <div style="margin-top:8px;color:#ffb020;line-height:1.6;">
          注意：上面是「这台电脑」的信息。<br/>
          对方 IP 要靠「扫描对方电脑」出现，或看对方自己面板上的电脑名+IP（两边不应完全一样）。
        </div>
      </div>
      <div class="status" id="linkStatus"></div>
      <div id="peerBox" style="margin-top:12px;"></div>
      <p class="hint">
        1. 两台电脑连同一个 WiFi，两边都运行「启动接收端.bat」<br/>
        2. 点「扫描对方电脑」→ 列表里会出现对方 IP（点一下即可填入）<br/>
        3. 对方电脑自己的浏览器面板上，也会显示对方自己的大号本机 IP
      </p>
    </div>

    <div class="card">
      <div class="drop" id="drop">
        <strong>把文件拖到这里</strong>
        或点击选择（支持多选，异步并行发送）
      </div>
      <input id="file" type="file" multiple />
      <div class="file-list" id="fileList"></div>
      <button type="button" class="send" id="btnSend" disabled>发送到对方电脑</button>
      <div class="bar" id="bar"><i id="barInner"></i></div>
      <div class="status" id="msg"></div>
    </div>
  </div>

  <script>
    const MAX_PARALLEL = 4; // 电脑网速通常更好，并行多一点
    const targetIp = document.getElementById('targetIp');
    const linkStatus = document.getElementById('linkStatus');
    const drop = document.getElementById('drop');
    const fileInput = document.getElementById('file');
    const fileList = document.getElementById('fileList');
    const btnSend = document.getElementById('btnSend');
    const msg = document.getElementById('msg');
    const bar = document.getElementById('bar');
    const barInner = document.getElementById('barInner');
    let selected = [];
    let sending = false;
    let targetBase = '';

    function fmtSize(n) {
      if (n < 1024) return n + ' B';
      if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
      return (n/1024/1024).toFixed(1) + ' MB';
    }

    function buildBase(ip) {
      ip = (ip || '').trim().replace(/^https?:\\/\\//, '').replace(/\\/$/, '');
      if (!ip) return '';
      if (ip.indexOf(':') === -1) ip = ip + ':8765';
      return 'http://' + ip;
    }

    async function testLink() {
      targetBase = buildBase(targetIp.value);
      if (!targetBase) {
        linkStatus.className = 'status bad';
        linkStatus.textContent = '请先填写对方 IP';
        return false;
      }
      linkStatus.className = 'status run';
      linkStatus.textContent = '正在检测 ' + targetBase + ' ...';
      try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 4000);
        const res = await fetch(targetBase + '/ping', { signal: ctrl.signal });
        clearTimeout(t);
        const data = await res.json();
        if (data.ok) {
          linkStatus.className = 'status ok';
          linkStatus.textContent = '在线 ✓ 可以发送  · 对方保存目录：' + (data.save_dir || '');
          localStorage.setItem('pc_peer_ip', targetIp.value.trim());
          return true;
        }
        throw new Error('not ok');
      } catch (e) {
        linkStatus.className = 'status bad';
        linkStatus.textContent = '连不上：请确认对方已启动接收端、同一 WiFi、防火墙已放行 8765';
        return false;
      }
    }

    function renderList() {
      fileList.innerHTML = '';
      selected.forEach((item, idx) => {
        const row = document.createElement('div');
        row.className = 'file-row';
        row.innerHTML =
          '<div><div class="fname"></div>' +
          '<div class="fsub" id="sub-' + idx + '">待发送 · ' + fmtSize(item.file.size) + '</div>' +
          '<div class="mini"><i id="p-' + idx + '"></i></div></div>';
        row.querySelector('.fname').textContent = item.file.name;
        fileList.appendChild(row);
      });
      btnSend.disabled = selected.length === 0 || sending;
    }

    function takeFiles(list) {
      if (sending) return;
      selected = Array.from(list || []).map((file) => ({ file, status: 'pending' }));
      renderList();
      msg.textContent = selected.length ? ('已选 ' + selected.length + ' 个文件') : '';
      msg.className = 'status';
    }

    function setStatus(idx, text, cls, percent) {
      const sub = document.getElementById('sub-' + idx);
      const p = document.getElementById('p-' + idx);
      if (sub) { sub.textContent = text; sub.className = 'fsub' + (cls ? ' ' + cls : ''); }
      if (p && typeof percent === 'number') p.style.width = Math.max(0, Math.min(100, percent)) + '%';
    }

    function refreshOverall() {
      if (!selected.length) return;
      let sum = 0;
      selected.forEach((item, idx) => {
        const p = document.getElementById('p-' + idx);
        const w = p ? parseFloat(p.style.width) || 0 : 0;
        sum += (item.status === 'ok' || item.status === 'bad') ? 100 : w;
      });
      barInner.style.width = Math.round(sum / selected.length) + '%';
    }

    function uploadOne(item, idx) {
      return new Promise((resolve) => {
        const fd = new FormData();
        fd.append('file', item.file);
        const xhr = new XMLHttpRequest();
        xhr.open('POST', targetBase + '/upload');
        setStatus(idx, '上传中 0%', 'run', 0);
        xhr.upload.onprogress = (e) => {
          if (!e.lengthComputable) return;
          const pct = Math.round(e.loaded / e.total * 100);
          setStatus(idx, '上传中 ' + pct + '% · ' + fmtSize(item.file.size), 'run', pct);
          refreshOverall();
        };
        xhr.onload = () => {
          try {
            const data = JSON.parse(xhr.responseText);
            if (xhr.status >= 200 && xhr.status < 300 && data.ok) {
              item.status = 'ok';
              const saved = (data.saved && data.saved[0]) || item.file.name;
              setStatus(idx, '完成 · ' + saved, 'ok', 100);
              resolve({ ok: true, saved });
            } else {
              item.status = 'bad';
              setStatus(idx, '失败：' + ((data && data.error) || xhr.status), 'bad', 0);
              resolve({ ok: false });
            }
          } catch (e) {
            item.status = 'bad';
            setStatus(idx, '失败：返回异常', 'bad', 0);
            resolve({ ok: false });
          }
          refreshOverall();
        };
        xhr.onerror = () => {
          item.status = 'bad';
          setStatus(idx, '失败：网络错误', 'bad', 0);
          refreshOverall();
          resolve({ ok: false });
        };
        xhr.send(fd);
      });
    }

    async function uploadAllAsync(items) {
      const results = new Array(items.length);
      let next = 0;
      async function worker() {
        while (next < items.length) {
          const i = next++;
          results[i] = await uploadOne(items[i], i);
        }
      }
      const n = Math.min(MAX_PARALLEL, items.length);
      await Promise.all(Array.from({ length: n }, () => worker()));
      return results;
    }

    document.getElementById('btnTest').onclick = testLink;

    document.getElementById('btnScan').onclick = async () => {
      const box = document.getElementById('peerBox');
      linkStatus.className = 'status run';
      linkStatus.textContent = '正在扫描局域网，请稍候（大约几秒到十几秒）...';
      box.innerHTML = '';
      try {
        const res = await fetch('/api/discover');
        const data = await res.json();
        if (!data.ok) throw new Error('scan failed');
        const peers = data.peers || [];
        if (!peers.length) {
          linkStatus.className = 'status bad';
          linkStatus.textContent = '没扫到其他电脑。请确认：对方已启动接收端、同一 WiFi、防火墙已放行';
          box.innerHTML = '<div class="status bad">对方电脑浏览器里也会显示它自己的本机 IP，可让对方念给你听后手动填写。</div>';
          return;
        }
        linkStatus.className = 'status ok';
        linkStatus.textContent = '发现 ' + peers.length + ' 台对方电脑，点击即可填入 IP';
        box.innerHTML = peers.map((p) => {
          const name = p.hostname || p.ip;
          return '<div class="peer" data-ip="' + p.ip + '">' +
            '<div><b>' + p.ip + '</b><div style="margin-top:4px;color:#e8f7ff;font-size:12px;">电脑名：' + name + '</div>' +
            '<div style="margin-top:4px;color:#7aa0b8;font-size:11px;">点击选用 · ' + (p.message || '在线') + '</div></div>' +
            '<span>PORT ' + {{ port|tojson }} + '</span></div>';
        }).join('');
        box.querySelectorAll('.peer').forEach((el) => {
          el.onclick = () => {
            targetIp.value = el.dataset.ip;
            testLink();
          };
        });
      } catch (e) {
        linkStatus.className = 'status bad';
        linkStatus.textContent = '扫描失败，请重试';
      }
    };

    drop.onclick = () => { if (!sending) fileInput.click(); };
    fileInput.onchange = () => takeFiles(fileInput.files);
    ;['dragenter','dragover'].forEach((ev) => {
      drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('drag'); });
    });
    ;['dragleave','drop'].forEach((ev) => {
      drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('drag'); });
    });
    drop.addEventListener('drop', (e) => takeFiles(e.dataTransfer.files));

    btnSend.onclick = async () => {
      if (!selected.length || sending) return;
      const ok = await testLink();
      if (!ok) return;
      sending = true;
      btnSend.disabled = true;
      msg.className = 'status run';
      msg.textContent = '正在发往 ' + targetBase + ' （并行 ' + Math.min(MAX_PARALLEL, selected.length) + '）...';
      bar.style.display = 'block';
      barInner.style.width = '0%';
      const results = await uploadAllAsync(selected);
      const okN = results.filter((r) => r && r.ok).length;
      const badN = results.length - okN;
      sending = false;
      btnSend.disabled = false;
      if (badN === 0) {
        msg.className = 'status ok';
        msg.textContent = '全部发送成功（' + okN + ' 个），请到对方电脑查看 received 文件夹';
      } else if (okN === 0) {
        msg.className = 'status bad';
        msg.textContent = '全部失败，请检查对方接收端 / 防火墙';
      } else {
        msg.className = 'status bad';
        msg.textContent = '部分成功：成功 ' + okN + '，失败 ' + badN;
      }
    };

    // 记住上次对方 IP
    const saved = localStorage.getItem('pc_peer_ip');
    if (saved) targetIp.value = saved;

    document.getElementById('btnCopyMyIp').onclick = async () => {
      const ip = {{ my_ip|tojson }};
      try {
        await navigator.clipboard.writeText(ip);
        linkStatus.className = 'status ok';
        linkStatus.textContent = '本机 IP 已复制：' + ip;
      } catch (e) {
        prompt('请手动复制本机 IP：', ip);
      }
    };
  </script>
</body>
</html>
"""


@app.get("/")
def home_phone():
    """手机打开的上传页。"""
    return render_template_string(PHONE_PAGE)


@app.get("/pc")
def home_pc():
    """电脑端面板：二维码 + 最近文件。"""
    wifi = get_wifi_config()
    return render_template_string(
        PC_PAGE,
        url=phone_url(),
        ip=get_lan_ip(),
        hostname=get_hostname(),
        save_dir=str(SAVE_DIR),
        wifi_ssid=wifi.get("ssid") or "",
        wifi_password=wifi.get("password") or "",
    )


@app.get("/send")
def home_send():
    """电脑互传：把文件发到另一台电脑。"""
    return render_template_string(
        SEND_PAGE,
        my_url=phone_url(),
        my_ip=get_lan_ip(),
        hostname=get_hostname(),
        port=PORT,
    )


@app.get("/api/discover")
def api_discover():
    """扫描局域网，列出其他已启动接收端的电脑 IP。"""
    peers = discover_peers()
    return jsonify({"ok": True, "my_ip": get_lan_ip(), "peers": peers, "count": len(peers)})


@app.get("/qr.png")
def qr_png():
    """生成手机访问地址的二维码图片。"""
    img = qrcode.make(phone_url())
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/wifi-qr.png")
def wifi_qr_png():
    """生成「一键连接 WiFi」二维码（系统相机可识别）。"""
    wifi = get_wifi_config()
    ssid = wifi.get("ssid") or ""
    if not ssid:
        abort(404)
    payload = build_wifi_qr_payload(ssid, wifi.get("password") or "", wifi.get("auth") or "WPA")
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/api/wifi-config")
def api_wifi_config_get():
    wifi = get_wifi_config()
    # 不在接口里回传完整密码给远程访客（仅本机面板用表单展示）
    return jsonify({"ok": True, "ssid": wifi.get("ssid") or "", "has_password": bool(wifi.get("password"))})


@app.post("/api/wifi-config")
def api_wifi_config_set():
    """保存 WiFi 预设，供生成连网二维码。建议仅在本机操作。"""
    remote = (request.remote_addr or "").strip()
    if remote not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "error": "请在本机面板设置 WiFi（打开 http://127.0.0.1:8765/pc）"}), 403
    data = request.get_json(silent=True) or {}
    ssid = str(data.get("ssid") or "").strip()
    if not ssid:
        return jsonify({"ok": False, "error": "WiFi 名称不能为空"}), 400
    password = str(data.get("password") or "")
    auth = str(data.get("auth") or "WPA")
    cfg = set_wifi_config(ssid, password, auth)
    return jsonify({"ok": True, "ssid": cfg["ssid"]})


@app.get("/api/wifi-detect")
def api_wifi_detect():
    """尝试读取本机当前 WiFi 名称（密码仍需手动填）。"""
    ssid = detect_current_wifi_ssid()
    if not ssid:
        return jsonify({"ok": False, "error": "未检测到 WiFi（电脑是否用网线？或请手动填写名称）"})
    return jsonify({"ok": True, "ssid": ssid})


@app.get("/ping")
def ping():
    return jsonify(
        {
            "ok": True,
            "message": "电脑接收端在线",
            "save_dir": str(SAVE_DIR),
            "url": phone_url(),
            "ip": get_lan_ip(),
            "hostname": get_hostname(),
        }
    )


@app.get("/api/info")
def api_info():
    return jsonify(
        {
            "ok": True,
            "ip": get_lan_ip(),
            "hostname": get_hostname(),
            "port": PORT,
            "url": phone_url(),
            "save_dir": str(SAVE_DIR),
        }
    )


@app.get("/api/files")
def api_files():
    return jsonify({"ok": True, "files": list_recent_files()})


@app.get("/file/<path:filename>")
def serve_file(filename: str):
    """
    安全地提供 received 目录里的文件，供电脑端预览用。
    只允许访问保存目录内的文件，防止读到别的路径。
    """
    # 只取文件名，去掉任何路径穿越（如 ../）
    safe = Path(filename).name
    target = (SAVE_DIR / safe).resolve()
    if not str(target).startswith(str(SAVE_DIR.resolve())):
        abort(404)
    if not target.is_file():
        abort(404)
    # as_attachment=False：浏览器里直接预览，而不是强制下载
    return send_file(target, as_attachment=False, conditional=True)


@app.post("/api/open-folder")
def api_open_folder():
    """在资源管理器中打开当前保存文件夹。"""
    os.startfile(SAVE_DIR)  # Windows 专用
    return jsonify({"ok": True, "save_dir": str(SAVE_DIR)})


@app.post("/api/choose-save-dir")
def api_choose_save_dir():
    """
    弹出系统文件夹选择框，让接收方自己选保存位置。
    只允许在本机浏览器（127.0.0.1）触发，避免对方点按钮却弹到你屏幕上。
    """
    remote = (request.remote_addr or "").strip()
    if remote not in ("127.0.0.1", "::1"):
        return jsonify(
            {
                "ok": False,
                "error": "请在本机打开 http://127.0.0.1:8765/pc 再选择保存位置（不要打开别人的 IP 地址）",
            }
        ), 403
    try:
        chosen = pick_save_folder_dialog()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not chosen:
        return jsonify({"ok": False, "cancelled": True})
    path = set_save_dir(Path(chosen))
    print(f"[配置] 保存目录已改为：{path}")
    return jsonify({"ok": True, "save_dir": str(path)})


@app.post("/api/set-save-dir")
def api_set_save_dir():
    """也可手动提交路径（高级用法）。"""
    data = request.get_json(silent=True) or {}
    raw = str(data.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "缺少 path"}), 400
    try:
        path = set_save_dir(Path(raw))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "save_dir": str(path)})


@app.post("/api/open-file")
def api_open_file():
    """用系统默认程序打开某个已收到的文件。"""
    data = request.get_json(silent=True) or {}
    name = Path(str(data.get("name", ""))).name
    if not name:
        return jsonify({"ok": False, "error": "缺少文件名"}), 400
    target = (SAVE_DIR / name).resolve()
    if not str(target).startswith(str(SAVE_DIR.resolve())) or not target.is_file():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    os.startfile(target)
    return jsonify({"ok": True})


@app.post("/upload")
def upload():
    """
    接收手机发来的文件。
    支持单文件（异步多文件时每个请求一个）和一次多个 files。
    """
    files = request.files.getlist("files")
    if not files:
        one = request.files.get("file")
        files = [one] if one else []

    if not files:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    saved_names = []
    for idx, f in enumerate(files):
        if not f or not f.filename:
            continue
        safe_name = Path(f.filename).name
        # 毫秒时间戳：异步并发上传时也不会重名覆盖
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = SAVE_DIR / f"{stamp}_{idx+1}_{safe_name}"
        f.save(target)
        saved_names.append(target.name)
        print(f"[已保存] {target} ({human_size(target.stat().st_size)})")

    if not saved_names:
        return jsonify({"ok": False, "error": "文件名为空"}), 400

    # 弹通知（放到后台线程，避免拖慢上传响应）
    threading.Thread(target=notify_received, args=(saved_names,), daemon=True).start()

    return jsonify({"ok": True, "saved": saved_names, "count": len(saved_names)})


def open_pc_panel_later(url: str) -> None:
    """服务启动稍等片刻后，自动打开电脑端面板。"""
    time.sleep(1.2)
    webbrowser.open(url)


def main():
    lan_ip = get_lan_ip()
    phone = f"http://{lan_ip}:{PORT}"
    panel = f"http://127.0.0.1:{PORT}/pc"
    send = f"http://127.0.0.1:{PORT}/send"

    print("=" * 56)
    print("  局域网传文件 · 收发端已启动")
    print("=" * 56)
    print(f"  保存目录：{SAVE_DIR}")
    print(f"  本机地址：{phone}")
    print(f"  接收面板：{panel}")
    print(f"  电脑互传：{send}")
    print()
    print("  手机→电脑：扫码 / 打开本机地址")
    print("  电脑→电脑：两边都启动本程序，打开「电脑互传」填对方 IP")
    print("  按 Ctrl+C 停止")
    print("=" * 56)

    threading.Thread(target=open_pc_panel_later, args=(panel,), daemon=True).start()
    # threaded=True：电脑面板会定时刷新，同时还能上传，互不卡住
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
