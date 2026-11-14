#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes 远程对话网站 —— 只读 state.db 浏览全部会话 + 通过 hermes -z 续写对话。

设计要点：
- 只绑定 127.0.0.1，由 tailscale serve 反向代理对外（仅尾内网可达）。
- 登录复用官方面板 config.yaml 里 dashboard.basic_auth 的账号密码（scrypt 哈希校验）。
- 浏览：只读连接 state.db（WAL，可与桌面端并发读）。
- 续写：后台线程执行 hermes -z "<文本>" --resume <会话ID>，消息实时写入同一
  state.db，桌面端重新打开该对话即可看到；网页端靠轮询 messages 表增量展示。
"""
import base64
import gzip
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import secrets
import socketserver
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
LOG_FILE = APP_DIR / "server.log"
PID_FILE = APP_DIR / "server.pid"
UPLOADS_DIR = APP_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _read_env_dict() -> dict:
    """解析 APP_DIR/.env 为键值对字典。"""
    env_file = APP_DIR / ".env"
    data = {}
    if not env_file.is_file():
        return data
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k:
                data[k] = v
    except Exception:
        pass
    return data


def load_env_file(override: bool = False) -> None:
    """轻量加载 APP_DIR 下的 .env 环境变量（纯标准库，无需外部依赖）。"""
    for k, v in _read_env_dict().items():
        if override or k not in os.environ:
            os.environ[k] = v


load_env_file()

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
DEFAULT_HERMES_HOME = Path(LOCALAPPDATA) / "hermes" if LOCALAPPDATA else Path.home() / ".hermes"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", DEFAULT_HERMES_HOME)).expanduser().resolve()
STATE_DB = HERMES_HOME / "state.db"
CONFIG_YAML = HERMES_HOME / "config.yaml"

def _resolve_hermes_bin(home: Path) -> Path:
    candidates = [
        home / "hermes-agent" / ".hermes" / "bin" / "hermes.exe",
        home / "bin" / "hermes.exe",
        home / "bin" / "hermes",
    ]
    for c in candidates:
        if c.exists():
            return c
    import shutil
    w = shutil.which("hermes.exe") or shutil.which("hermes")
    if w:
        return Path(w)
    return candidates[0]

HERMES_BIN = _resolve_hermes_bin(HERMES_HOME)

PORT = int(os.environ.get("PORT", "9220") or "9220")
BIND = "127.0.0.1"
BASE_PREFIX = "/app"          # tailscale serve --set-path=/app 的挂载前缀（兼容被剥离/不被剥离两种情况）
JOB_TIMEOUT = 900             # 单次回合最长 15 分钟
MAX_JOBS = 3                  # 全局并发回合上限
COOKIE_NAME = "hermes_web_session"
COOKIE_TTL = 365 * 24 * 3600   # 登录态长期有效（保持一直登录）
UPLOAD_TTL_DAYS = 30           # 上传附件保留天数，超期后台自动清理
UPLOAD_CLEAN_INTERVAL = 6 * 3600

# 可通过 /api/file 预览的媒体扩展名（防止任意文件泄露）
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif",
             ".mp4", ".webm", ".mov", ".mp3", ".wav", ".m4a", ".flac", ".pdf",
             ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv", ".zip"}
# 绝不允许下载的敏感文件名
SENSITIVE_NAME = {"config.yaml", ".env", "auth.json", "state.db", "credentials",
                  "id_rsa", "id_ed25519"}

JOBS = {}
_STATIC_CACHE = {}                 # 静态文件内存缓存：name -> (mtime_ns, 原文, gzip 版本)


def _compressible(ctype: str) -> bool:
    c = (ctype or "").lower()
    return c.startswith("text/") or any(k in c for k in ("json", "javascript", "xml", "manifest"))


def _parse_byte_range(range_hdr: str, file_size: int) -> tuple[int, int, int]:
    """Parse a single HTTP Range header into (status, start, end) with 200/206/416."""
    hdr = range_hdr.strip()
    if not hdr.lower().startswith("bytes=") or "," in hdr:
        return (200, 0, max(0, file_size - 1))
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", hdr, re.I)
    if not m or (not m.group(1) and not m.group(2)) or file_size <= 0:
        return (416, 0, 0)
    s_str, e_str = m.group(1), m.group(2)
    if not s_str:
        suffix = int(e_str)
        if suffix <= 0:
            return (416, 0, 0)
        return (206, max(0, file_size - suffix), file_size - 1)
    start = int(s_str)
    end = min(int(e_str), file_size - 1) if e_str else (file_size - 1)
    if start >= file_size or start > end:
        return (416, 0, 0)
    return (206, start, end)


def _cleanup_uploads():
    """清理超过 UPLOAD_TTL_DAYS 天的上传附件，避免 uploads/ 无限增长。"""
    cutoff = time.time() - UPLOAD_TTL_DAYS * 86400
    removed = 0
    for f in UPLOADS_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        log(f"uploads cleanup: removed {removed} files older than {UPLOAD_TTL_DAYS} days")


def _cleanup_loop():
    while True:
        try:
            _cleanup_uploads()
        except Exception as exc:
            log(f"uploads cleanup error: {exc}")
        time.sleep(UPLOAD_CLEAN_INTERVAL)


JOBS_LOCK = threading.Lock()
SESSION_QUEUE = {}                 # 排队中的回合：qkey(session_id 或 new-*) -> [job_id]
SESSION_JOB = {}                   # session_id -> job_id（同一会话同时只跑一个回合）
CLAIMED_SIDS = set()               # 已被新建回合认领的会话 id，防止两个回合抢同一个


# ---------------------------------------------------------------- 桌面偏好读取
# Hermes 桌面客户端把「模型勾选(蓝色开关)」「思考强度」等偏好存在
# Electron Local Storage（leveldb）里。这里以只读方式解析，供网页端同步显示。
_LS_DIR = Path(os.environ.get("APPDATA", "")) / "Hermes" / "Local Storage" / "leveldb"


def _varint(data, i):
    v = 0; shift = 0
    while True:
        if i >= len(data):
            raise ValueError("varint overflow")
        b = data[i]; i += 1
        v |= (b & 0x7f) << shift
        if not b & 0x80:
            return v, i
        shift += 7


def _snappy_dec(data):
    i = 0
    _ln, i = _varint(data, i)
    out = bytearray()
    while i < len(data):
        tag = data[i]; i += 1
        t = tag & 3
        if t == 0:
            l = tag >> 2
            if l < 60:
                ln = l + 1
            else:
                nb = l - 59
                ln = int.from_bytes(data[i:i + nb], 'little') + 1; i += nb
            out += data[i:i + ln]; i += ln
        else:
            if t == 1:
                ln = ((tag >> 2) & 7) + 4
                off = ((tag >> 5) << 8) | data[i]; i += 1
            elif t == 2:
                ln = (tag >> 2) + 1
                off = int.from_bytes(data[i:i + 2], 'little'); i += 2
            else:
                ln = (tag >> 2) + 1
                off = int.from_bytes(data[i:i + 4], 'little'); i += 4
            start = len(out) - off
            if start < 0:
                raise ValueError("snappy bad offset")
            for k in range(ln):
                out.append(out[start + k])
    return bytes(out)


def _ldb_entries(path):
    """SSTable 内部键 = 用户键 + 8 字节序号尾缀；返回 {用户键: (seq, 值)}。"""
    data = Path(path).read_bytes()
    if len(data) < 48:
        return {}
    footer = data[-48:]
    try:
        _mo, i = _varint(footer, 0)
        _ms, i = _varint(footer, i)
        io, i = _varint(footer, i)
        isz, i = _varint(footer, i)
    except ValueError:
        return {}

    def read_block(off, size):
        seg = data[off:off + size + 5]
        body, typ = seg[:size], seg[size] if len(seg) > size else 0
        return _snappy_dec(body) if typ == 1 else body

    def parse_block(bl):
        if len(bl) < 4:
            return []
        n = struct.unpack('<I', bl[-4:])[0]
        roff = len(bl) - 4 - 4 * n
        out = []
        i = 0
        prev = b''
        while 0 <= i < roff:
            try:
                shared, i = _varint(bl, i)
                nonshared, i = _varint(bl, i)
                vlen, i = _varint(bl, i)
            except ValueError:
                break
            key = prev[:shared] + bl[i:i + nonshared]; i += nonshared
            val = bl[i:i + vlen]; i += vlen
            out.append((key, val))
            prev = key
        return out

    result = {}
    try:
        idx = read_block(io, isz)
        for _k, handle in parse_block(idx):
            off, j = _varint(handle, 0)
            size, j = _varint(handle, j)
            for k, v in parse_block(read_block(off, size)):
                if len(k) < 8:
                    continue
                user, tail = k[:-8], k[-8:]
                packed = struct.unpack('<Q', tail)[0]
                seq, typ = packed >> 8, packed & 0xff
                if typ == 1:
                    old = result.get(user)
                    if old is None or seq >= old[0]:
                        result[user] = (seq, v)
    except Exception:
        pass
    return result


def _ldb_log_entries(path):
    """WAL(.log) 记录解析（最新未压缩写入），返回 {用户键: (seq, 值)}。"""
    data = Path(path).read_bytes()
    BLOCK = 32768
    result = {}
    pos = 0
    frag = b''

    def eat_batch(rec):
        if len(rec) < 12:
            return
        seq = struct.unpack('<Q', rec[0:8])[0]
        cnt = struct.unpack('<I', rec[8:12])[0]
        i = 12
        for _ in range(cnt):
            if i >= len(rec):
                return
            typ = rec[i]; i += 1
            try:
                klen, i = _varint(rec, i)
                key = rec[i:i + klen]; i += klen
                vlen, i = _varint(rec, i)
                val = rec[i:i + vlen]; i += vlen
            except ValueError:
                return
            if typ == 1 and len(key) >= 8:
                user = key[:-8]
                packed = struct.unpack('<Q', key[-8:])[0]
                s = packed >> 8
                old = result.get(user)
                if old is None or s >= old[0]:
                    result[user] = (s, val)

    while pos + 7 <= len(data):
        blk_end = (pos // BLOCK + 1) * BLOCK
        if pos + 7 > blk_end:
            pos = blk_end
            continue
        _crc, ln, typ = struct.unpack('<IHB', data[pos:pos + 7])
        if ln == 0 and typ == 0:
            pos = blk_end
            continue
        if pos + 7 + ln > min(blk_end, len(data)):
            break
        rec = data[pos + 7:pos + 7 + ln]
        pos += 7 + ln
        if typ == 1:
            eat_batch(rec)
        elif typ == 2:
            frag = rec
        elif typ == 3:
            frag += rec
        elif typ == 4:
            eat_batch(frag + rec)
            frag = b''
    return result


_PREFS_CACHE = {"t": 0.0, "data": None}
_PREFS_LOCK = threading.Lock()


def read_desktop_prefs(ttl=60.0):
    """读取桌面客户端偏好：勾选模型(蓝色开关) / 思考强度 / 收藏。读不到返回空。
    解析整个 leveldb 约 1 秒，使用 60s 内存缓存 + 锁，避免重复纯 Python 解压遍历。"""
    out = {"visible": [], "effort": "", "favorites": [], "ok": False}
    if not _LS_DIR.is_dir():
        return out
    now = time.time()
    if _PREFS_CACHE["data"] is not None and now - _PREFS_CACHE["t"] < ttl:
        return _PREFS_CACHE["data"]
    with _PREFS_LOCK:
        if _PREFS_CACHE["data"] is not None and time.time() - _PREFS_CACHE["t"] < ttl:
            return _PREFS_CACHE["data"]
        data = _read_desktop_prefs_uncached(out)
        _PREFS_CACHE["data"] = data
        _PREFS_CACHE["t"] = time.time()
        return data


def _read_desktop_prefs_uncached(out):
    entries = {}
    try:
        for f in sorted(_LS_DIR.iterdir()):
            try:
                if f.suffix == '.ldb':
                    part = _ldb_entries(f)
                elif f.suffix == '.log':
                    part = _ldb_log_entries(f)
                else:
                    continue
                for k, sv in part.items():
                    if k not in entries or sv[0] >= entries[k][0]:
                        entries[k] = sv
            except Exception:
                continue
    except OSError:
        return out
    want = {
        b'hermes.desktop.visible-models': 'visible',
        b'hermes.desktop.composer.reasoning-effort': 'effort',
        b'hermes.desktop.favorite-models': 'favorites',
    }
    chosen = {}
    for key, (seq, val) in entries.items():
        # 真实键带 `_file://\x00\x01` 前缀（Electron origin 标记），按后缀匹配
        name = next((v for k, v in want.items() if key.endswith(k)), None)
        if not name:
            continue
        if name not in chosen or seq >= chosen[name][0]:
            chosen[name] = (seq, val)
    for name, (seq, val) in chosen.items():
        text = val.decode('utf-8', 'replace').lstrip('\x01')
        try:
            if name == 'effort':
                out[name] = text.strip().lower()
            else:
                arr = json.loads(text)
                if isinstance(arr, list):
                    out[name] = [x for x in arr if isinstance(x, str)]
            out["ok"] = True
        except Exception:
            continue
    return out


def model_display_name(model: str) -> str:
    """模仿桌面端 displayModelName：去 provider 前缀、连字符转空格、Title Case、
    参数量后缀大写（31b→31B、a12b→A12B）、MiMo 等厂商大小写。"""
    base = model.rsplit('/', 1)[-1]
    base = re.sub(r'-\d{8}$', '', base)
    words = base.replace('-', ' ').replace('_', ' ').split()
    out = []
    for w in words:
        w = re.sub(r'\b(a?)(\d+(?:\.\d+)?)b\b',
                   lambda mm: mm.group(1).upper() + mm.group(2) + 'B', w, flags=re.I)
        w = w[:1].upper() + w[1:] if w else w
        out.append(w)
    titled = ' '.join(out)
    titled = re.sub(r'\bMimo\b', 'MiMo', titled)
    return titled or model

def _s(v) -> str:
    """JSON 值防抖：非字符串一律视为空串（避免 int/dict.strip() 崩掉请求线程）。"""
    return v if isinstance(v, str) else ""


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


_AUTH_CACHE = {"mtime": None, "cfg": None}
_AUTH_LOCK = threading.Lock()


def _stat_mtime(p: Path):
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def load_basic_auth_cfg() -> dict:
    """带双源 mtime 缓存的鉴权配置读取：仅当 config.yaml 或 .env 变化时才重新解析。"""
    mt = (_stat_mtime(CONFIG_YAML), _stat_mtime(APP_DIR / ".env"))
    with _AUTH_LOCK:
        if _AUTH_CACHE["cfg"] is not None and _AUTH_CACHE["mtime"] == mt:
            return dict(_AUTH_CACHE["cfg"])
        cfg = _parse_basic_auth_cfg()
        _AUTH_CACHE["mtime"] = mt
        _AUTH_CACHE["cfg"] = cfg
        return dict(cfg)


def _parse_basic_auth_cfg() -> dict:
    """从 config.yaml 与 .env / 环境变量读取鉴权配置（用户名/口令哈希/签名密钥）。

    YAML 值可能是单引号、双引号或无引号（hermes 改写 config.yaml 后引号风格会变），
    三种都要兼容。
    """
    cfg = {"username": "hermes", "secret": "", "password": "", "password_hash": ""}
    try:
        text = CONFIG_YAML.read_text(encoding="utf-8")
    except OSError:
        text = ""
    m = re.search(r"basic_auth:\s*\n((?:\s+[a-z_]+:\s*.+\n)+)", text)
    block = m.group(1) if m else ""
    for key in ("username", "secret", "password", "password_hash"):
        km = re.search(rf"{key}:\s*(?:'([^']*)'|\"((?:[^\"\\\\]|\\\\.)*)\"|(\S+))", block)
        if km:
            cfg[key] = next(g for g in km.groups() if g is not None)

    # 动态读取 .env 与环境变量覆盖（支持免重启热感知）及开箱默认兜底
    dotenv = _read_env_dict()
    env_user = dotenv.get("HERMES_WEB_USER") or os.environ.get("HERMES_WEB_USER")
    env_pass = dotenv.get("HERMES_WEB_PASSWORD") or os.environ.get("HERMES_WEB_PASSWORD")
    if env_user:
        cfg["username"] = env_user
    if env_pass:
        cfg["password"] = env_pass
        cfg["password_hash"] = ""
    if not cfg["password"] and not cfg["password_hash"]:
        cfg["password"] = "hermes123"
    if not cfg["secret"]:
        cfg["secret"] = "hermes-web-remote-secret-key-2026"
    return cfg


def verify_password(cfg: dict, username: str, password: str) -> bool:
    if not hmac.compare_digest(username, cfg["username"]):
        return False
    if cfg.get("password") and hmac.compare_digest(password, cfg["password"]):
        return True
    h = cfg.get("password_hash")
    if h:
        try:
            algo, n, r, p, salt_b64, hash_b64 = h.split("$")
            if algo != "scrypt":
                return False
            salt = base64.b64decode(salt_b64)
            want = base64.b64decode(hash_b64)
            got = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                 n=int(n), r=int(r), p=int(p), dklen=len(want))
            return hmac.compare_digest(got, want)
        except Exception:
            return False
    return False


def make_cookie(cfg: dict, username: str) -> str:
    exp = int(time.time()) + COOKIE_TTL
    payload = f"{exp}.{username}"
    sig = hmac.new(cfg["secret"].encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def check_cookie(cfg: dict, value: str) -> bool:
    try:
        exp_s, username, sig = value.split(".", 2)
        payload = f"{exp_s}.{username}"
        want = hmac.new(cfg["secret"].encode("utf-8"), payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return False
        return int(exp_s) > time.time() and username == cfg["username"]
    except Exception:
        return False


def normalize_path(path: str) -> str:
    """兼容 tailscale serve 剥离/保留挂载前缀两种转发方式。"""
    for prefix in (BASE_PREFIX,):
        if path == prefix or path == prefix + "/":
            return "/"
        if path.startswith(prefix + "/"):
            path = path[len(prefix):]
    return path


def db():
    conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=8)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def sanitize_session_title(raw_text: str) -> str:
    """净化会话标题：移除 @file: 路径为 [文件] 文件名，移除 [Image attached...] 为 [图片]"""
    if not raw_text:
        return ""
    s = raw_text.strip()
    m_file = re.search(r"@file:(?:`([^`\r\n]+)`|([^\s\r\n]+))", s)
    if m_file:
        raw_path = m_file.group(1) or m_file.group(2) or ""
        fname = Path(raw_path).name or "附件文件"
        rest = re.sub(r"@file:(?:`[^`\r\n]+`|[^\s\r\n]+)", "", s).strip()
        if rest:
            return f"[文件] {fname}: {rest}"[:40].replace("\n", " ")
        return f"[文件] {fname}"[:40]
    if re.search(r"\[Image attached at:|image_url:|@image:", s, re.I):
        rest = re.sub(r"\[Image attached at:[^\]]+\]", "", s)
        rest = re.sub(r"image_url:[^\s~\]\r\n]+", "", rest)
        rest = re.sub(r"@image:[^\s\]\r\n]+", "", rest).strip()
        if rest:
            return f"[图片] {rest}"[:40].replace("\n", " ")
        return "[图片] 图像分析"
    return s[:40].replace("\n", " ")


def row_to_msg(r, full=False) -> dict:
    content = r["content"] or ""
    truncated = False
    if not full and len(content) > 12000:
        content = content[:12000]
        truncated = True
    tool_calls = None
    if r["tool_calls"]:
        try:
            raw = json.loads(r["tool_calls"])
            tool_calls = []
            for tc in raw:
                fn = tc.get("function") or {}
                tool_calls.append({"name": fn.get("name") or tc.get("name") or "?",
                                   "arguments": (fn.get("arguments") or "")[:600]})
        except Exception:
            tool_calls = None
    reasoning = (r["reasoning"] or r["reasoning_content"] or "").strip()
    return {
        "id": r["id"], "role": r["role"], "content": content,
        "reasoning": reasoning or None,
        "tool_name": r["tool_name"], "tool_calls": tool_calls,
        "timestamp": r["timestamp"], "display_kind": r["display_kind"],
        "truncated": truncated,
    }


# ---------------------------------------------------------------- 任务执行

def _discover_new_session_id(before_ids, t0, expect_text=None):
    """认领本次回合新建的会话 id：t0 之后新出现、且首条用户消息以 expect_text 开头
    （防止把 cron/桌面端同窗口创建的会话认领成自己的）。expect_text 为空时
    退回“任意新会话”（仅图片/文件发送时无文本可比对）。"""
    row_only = []
    try:
        with db() as conn:
            if expect_text:
                # 先按整条内容精确匹配（并发新建相同文本时配合 CLAIMED_SIDS 去重），
                # 不中再退回前缀匹配（兼容 hermes 拼入 @file: 等前缀的情况）
                rows = conn.execute(
                    "SELECT m.session_id FROM messages m "
                    "JOIN sessions s ON s.id = m.session_id "
                    "WHERE m.role='user' AND s.started_at >= ? AND m.content = ? "
                    "ORDER BY m.id DESC LIMIT 8", (t0 - 5, expect_text)).fetchall()
                if not rows:
                    prefix = (expect_text[:40].replace("\\", "\\\\")
                              .replace("%", "\\%").replace("_", "\\_"))
                    rows = conn.execute(
                        "SELECT m.session_id FROM messages m "
                        "JOIN sessions s ON s.id = m.session_id "
                        "WHERE m.role='user' AND s.started_at >= ? "
                        "AND m.content LIKE ? ESCAPE '\\' "
                        "ORDER BY m.id DESC LIMIT 8", (t0 - 5, prefix + "%")).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM sessions WHERE started_at >= ? "
                    "ORDER BY started_at DESC LIMIT 20", (t0 - 5,)).fetchall()
            # 消息还没落库时的提前认领：窗口内只出现唯一新会话行才认（防误领）
            row_only = conn.execute(
                "SELECT id FROM sessions WHERE started_at >= ? "
                "ORDER BY started_at DESC LIMIT 20", (t0 - 1,)).fetchall()
        for r in rows:
            if r[0] not in before_ids and r[0] not in CLAIMED_SIDS:
                CLAIMED_SIDS.add(r[0])
                return r[0]
        cand = [r[0] for r in row_only
                if r[0] not in before_ids and r[0] not in CLAIMED_SIDS]
        if len(cand) == 1:
            CLAIMED_SIDS.add(cand[0])
            return cand[0]
    except Exception:
        pass
    return None


def run_turn(session_id, text, job_id, discover_new=False, model=None, effort=None, image=None, file=None):
    """在后台执行 hermes chat -q 回合。discover_new=True 时自动发现新建会话 id。"""
    job = JOBS[job_id]
    before_ids = set()
    t0 = time.time()
    if discover_new:
        try:
            with db() as conn:
                before_ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
        except Exception:
            pass
    # 构造 Prompt：如果有文件，自动注入 @file:`path` 供 Hermes 原生上下文引擎加载
    prompt_body = (text or "").strip()
    file_ref = ""
    if file and os.path.isfile(file):
        file_ref = f"@file:`{file}`"

    if file_ref and prompt_body:
        query_text = f"{file_ref}\n\n{prompt_body}"
    elif file_ref:
        if image:
            query_text = f"{file_ref}\n\n请结合此文件和图片进行分析"
        else:
            query_text = f"{file_ref}\n\n请阅读并分析此文件"
    elif image and not prompt_body:
        query_text = "请分析这张图片"
    else:
        query_text = prompt_body or "你好"

    cmd = [str(HERMES_BIN), "chat", "-q", query_text]
    if image and os.path.isfile(image):
        cmd += ["--image", str(image)]
    if session_id:
        cmd += ["--resume", session_id]
    else:
        # 显式来源标签：不指定时 chat -q 一律落 'oneshot'，被客户端会话栏与
        # 「会话(客户端)」分类隐藏。--source gui 为 Hermes 官方合法的界面来源枚举，
        # 底层网关校验通过后自动映射存储为 local 客户端会话，在 PC 客户端与网页端 100% 同步展示并打上 [本地] 标签
        cmd += ["--source", "gui"]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["--reasoning", effort]
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["PYTHONIOENCODING"] = "utf-8"
    log(f"job {job_id} start: resume={session_id or '(new)'} len(text)={len(query_text)} image={bool(image)} file={bool(file)}")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(HERMES_HOME), creationflags=0x08000000,
            text=True, encoding="utf-8", errors="replace")
        job["proc"] = proc
        # 新建会话 id 尽早发现（观察线程）；主流程用 communicate 排空管道并带截止
        # 时间——原实现 poll 死等且从不读管道，子进程输出一堵满就永久挂死
        def _watch():
            while proc.poll() is None:
                if discover_new and not job.get("session_id") and time.time() - t0 > 0.5:
                    sid = _discover_new_session_id(before_ids, t0, query_text)
                    if sid:
                        job["session_id"] = sid
                        # 认领后立即登记会话占用：后续消息进入排队而不是撞车
                        with JOBS_LOCK:
                            SESSION_JOB.setdefault(sid, job_id)
                time.sleep(0.7)
        if discover_new:
            threading.Thread(target=_watch, daemon=True).start()
        try:
            out, err = proc.communicate(timeout=JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, creationflags=0x08000000)
            except Exception:
                pass
            proc.kill()
            try:
                proc.communicate(timeout=15)
            except Exception:
                pass
            job["status"] = "error"
            job["error"] = f"回合超时（>{JOB_TIMEOUT} 秒），已终止"
            log(f"job {job_id} timeout")
            return
        combined = ((err or "") + "\n" + (out or "")).strip()
        # hermes 偶尔以退出码 0 返回错误文本（如 invalid_params），按输出兜底识别
        has_err_text = any(l.startswith("Error:") or l.startswith("hermes: error:")
                           for l in combined.splitlines())
        if proc.returncode != 0 or has_err_text:
            job["status"] = "error"
            job["error"] = combined[-1500:] or "hermes 退出码非 0"
        else:
            job["status"] = "done"
            # 兜底：回合结束后再确认一次新建会话 id
            if discover_new and not job.get("session_id"):
                job["session_id"] = _discover_new_session_id(before_ids, t0, query_text)
                if job.get("session_id"):
                    with JOBS_LOCK:
                        SESSION_JOB.setdefault(job["session_id"], job_id)
            if session_id:
                job["session_id"] = session_id
        log(f"job {job_id} end: {job['status']} rc={proc.returncode}")
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"执行失败：{exc}"
        log(f"job {job_id} fail: {exc}")
    finally:
        job["ended_at"] = time.time()
        job["proc"] = None  # 释放进程对象句柄与管道缓存
        with JOBS_LOCK:
            key = job.get("session_id") or session_id
            if key and SESSION_JOB.get(key) == job_id:
                SESSION_JOB.pop(key, None)
        _pump_queue()


def start_job(session_id, text, discover_new=False, model=None, effort=None, image=None, file=None) -> dict:
    """启动回合；会话正忙/并发满时不拒绝，而是入队（回复中也能继续发消息）。"""
    params = {"text": text, "discover_new": discover_new, "model": model,
              "effort": effort, "image": image, "file": file}
    with JOBS_LOCK:
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
        busy = bool(session_id and session_id in SESSION_JOB)
        job_id = secrets.token_hex(8)
        queued = busy or running >= MAX_JOBS
        job = {"id": job_id, "session_id": session_id,
               "status": "queued" if queued else "running",
               "started_at": time.time(), "ended_at": None, "error": None,
               "proc": None, "params": params}
        JOBS[job_id] = job
        if queued:
            qkey = session_id or ("new-" + job_id)
            SESSION_QUEUE.setdefault(qkey, []).append(job_id)
            return {"job_id": job_id, "queued": True}
        if session_id:
            SESSION_JOB[session_id] = job_id
    _start_job_thread(job)
    return {"job_id": job_id}


def _start_job_thread(job):
    p = job["params"]
    threading.Thread(
        target=run_turn,
        args=(job["session_id"], p["text"], job["id"], p["discover_new"],
              p["model"], p["effort"], p["image"], p["file"]),
        daemon=True).start()


def _pump_queue():
    """回合结束/槽位空出后，把排队中的回合放行（同会话保持先来后到），并修剪过期的历史 Job。"""
    with JOBS_LOCK:
        # 定期清理历史 Job 与认领 SIDS，防止长期 24/7 运行内存与句柄无界泄漏
        now = time.time()
        finished_jids = [
            jid for jid, j in JOBS.items()
            if j.get("status") in ("done", "error") and j.get("ended_at")
        ]
        # 删除结束超过 1 小时（3600秒）的已完成任务
        for jid in finished_jids:
            if now - JOBS[jid]["ended_at"] > 3600:
                JOBS.pop(jid, None)
        # 如果总任务数仍超过 100，按结束时间淘汰最老的完成任务
        if len(JOBS) > 100:
            remaining_finished = sorted(
                [j for j in JOBS.values() if j.get("status") in ("done", "error") and j.get("ended_at")],
                key=lambda x: x["ended_at"]
            )
            for j in remaining_finished[:len(JOBS) - 100]:
                JOBS.pop(j["id"], None)

        if len(CLAIMED_SIDS) > 300:
            CLAIMED_SIDS.clear()

        running = sum(1 for j in JOBS.values() if j["status"] == "running")
        for qkey in list(SESSION_QUEUE.keys()):
            if running >= MAX_JOBS:
                break
            q = SESSION_QUEUE.get(qkey) or []
            sid = None if qkey.startswith("new-") else qkey
            if sid and sid in SESSION_JOB:
                continue
            while q:
                jid = q.pop(0)
                job = JOBS.get(jid)
                if job and job["status"] == "queued":
                    job["status"] = "running"
                    if sid:
                        SESSION_JOB[sid] = jid
                    running += 1
                    _start_job_thread(job)
                    break
            if not q:
                SESSION_QUEUE.pop(qkey, None)


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HermesWeb/1.0"

    # ---- 基础设施
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8",
              extra=None):
        extra = dict(extra or {})
        # 文本类响应（接口 JSON 等）超过 1KB 且客户端支持时 gzip 压缩，省手机流量
        if (len(body) > 1024 and "Content-Encoding" not in extra
                and _compressible(ctype) and self._accepts_gzip()):
            body = gzip.compress(body, compresslevel=5)
            extra["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Vary", "Accept-Encoding")
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if getattr(self, "_head", False):
            return
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_HEAD(self):
        # 走 GET 全套路由但不写响应体（Content-Length 仍然正确）
        self._head = True
        self.do_GET()

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        # 30MB 文件 base64 后约 40MB；上限 42MB 与「30MB 上传」一致（原 35MB 使
        # >25MB 文件被静默丢弃并报误导性错误）
        if length > 42_000_000:
            self.close_connection = True
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _path(self):
        parsed = urllib.parse.urlparse(self.path)
        return normalize_path(urllib.parse.unquote(parsed.path)), parsed.query

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin in (f"http://{host}", f"https://{host}")

    def _authed(self) -> bool:
        cfg = load_basic_auth_cfg()
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        c = cookie.get(COOKIE_NAME)
        if c and check_cookie(cfg, c.value):
            self._user = cfg["username"]
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user_pass = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = user_pass.split(":", 1)
                if verify_password(cfg, username, password):
                    self._user = username
                    return True
            except Exception:
                pass
        return False

    def _require_auth(self) -> bool:
        if self._authed():
            return True
        self._json(401, {"error": "未登录或登录已过期"})
        return False

    # ---- 路由
    def do_GET(self):
        path, query = self._path()
        if path == "/api/health":
            return self._json(200, {"ok": True, "time": time.time()})
        if path == "/api/me":
            if not self._require_auth():
                return
            return self._json(200, {"user": getattr(self, "_user", "")})
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/sw.js":   # Service Worker 放在站点根（挂载前缀下），作用域才能覆盖整站
            return self._static("sw.js")
        if path.startswith("/api/"):
            if not self._require_auth():
                return
            return self._api_get(path, query)
        # SPA 页面（登录页本身不设防，API 全部要鉴权）
        return self._static("index.html")

    def do_POST(self):
        path, _ = self._path()
        if path == "/api/login":
            return self._login()
        if not self._require_auth():
            return
        if not self._origin_ok():
            return self._json(403, {"error": "跨站请求被拒绝"})
        if self.headers.get("X-Hermes-Web") != "1":
            return self._json(403, {"error": "缺少请求头校验"})
        if path == "/api/logout":
            return self._json(200, {"ok": True},
                              extra={"Set-Cookie": f"{COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
        body = self._read_body()
        if path == "/api/upload":
            return self._api_upload(body)
        if path.startswith("/api/sessions/") and path.endswith("/send"):
            return self._api_send(path, body)
        if path.startswith("/api/sessions/") and path.endswith("/rename"):
            return self._api_rename(path, body)
        if path.startswith("/api/sessions/") and path.endswith("/model"):
            return self._api_switch_model(path, body)
        if path.startswith("/api/sessions/") and path.endswith("/reasoning"):
            return self._api_switch_effort(path, body)
        if path == "/api/conversations":
            return self._api_new_conversation(body)
        return self._json(404, {"error": "接口不存在"})

    # ---- 静态
    def _static(self, name: str):
        name = name.strip("/")
        if not name:
            name = "index.html"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            return self._json(404, {"error": "非法路径"})
        fp = STATIC_DIR / name
        if not fp.is_file():
            return self._json(404, {"error": "文件不存在"})
        ext = name.rsplit(".", 1)[-1].lower()
        ctype = {"html": "text/html; charset=utf-8", "js": "application/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "png": "image/png", "svg": "image/svg+xml",
                 "ico": "image/x-icon",
                 "webmanifest": "application/manifest+json; charset=utf-8",
                 "json": "application/json; charset=utf-8"}.get(ext, "application/octet-stream")
        try:
            mt = fp.stat().st_mtime_ns
            ent = _STATIC_CACHE.get(name)
            if not ent or ent[0] != mt:
                raw = fp.read_bytes()
                gz = (gzip.compress(raw, compresslevel=6)
                      if len(raw) > 1024 and _compressible(ctype) else None)
                ent = (mt, raw, gz)
                _STATIC_CACHE[name] = ent
        except OSError:
            return self._json(404, {"error": "文件不存在"})
        _, raw, gz = ent
        # 页面本体与 Service Worker 必须每次校验；带版本号的第三方库/图标长期缓存
        if name in ("index.html", "sw.js"):
            cc = "no-cache" if name == "sw.js" else "no-store"
        elif ext == "webmanifest":
            cc = "public, max-age=3600"
        else:
            cc = "public, max-age=2592000"
        extra = {"Cache-Control": cc}
        body = raw
        if gz is not None and self._accepts_gzip():
            body = gz
            extra["Content-Encoding"] = "gzip"
        self._send(200, body, ctype, extra)

    # ---- 登录
    def _login(self):
        body = self._read_body()
        cfg = load_basic_auth_cfg()
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        if not verify_password(cfg, username, password):
            time.sleep(1.2)  # 防暴力破解
            return self._json(401, {"error": "用户名或密码错误"})
        cookie = make_cookie(cfg, username)
        log(f"login ok: {username}")
        return self._json(200, {"ok": True, "user": username},
                          extra={"Set-Cookie": f"{COOKIE_NAME}={cookie}; Max-Age={COOKIE_TTL}; "
                                               f"Path=/; HttpOnly; SameSite=Strict"})

    # ---- API
    def _api_get(self, path, query):
        qs = urllib.parse.parse_qs(query)
        def q(key, default=None):
            return (qs.get(key) or [default])[0]
        def qi(key, default):
            try:
                return int((qs.get(key) or [default])[0])
            except (TypeError, ValueError):
                return default

        if path == "/api/sessions":
            kw = (q("q") or "").strip()
            source = (q("source") or "").strip()
            limit = max(1, min(qi("limit", 50), 200))
            offset = max(0, qi("offset", 0))
            where, args = [], []
            if not source or source == "chats":
                # 与 Hermes 客户端会话栏 100% 对齐：只显示真实交互会话，排除定时、单次、子代理、隐藏及归档
                where.append("s.hidden = 0")
                where.append("s.archived = 0")
                where.append("s.source NOT IN ('cron', 'oneshot', 'subagent', 'kanban', 'acp', 'tool')")
            elif source == "archived":
                where.append("s.archived = 1")
            elif source == "all":
                pass
            elif source in ("local", "desktop"):
                where.append("s.archived = 0")
                where.append("s.source IN ('local', 'desktop', 'gui')")
            else:
                where.append("s.archived = 0")
                where.append("s.source = ?")
                args.append(source)

            if kw:
                like = f"%{kw}%"
                where.append("(s.title LIKE ? OR s.id IN "
                             "(SELECT session_id FROM messages WHERE content LIKE ? LIMIT 200))")
                args += [like, like]
            sql = ("SELECT s.id, s.title, s.source, s.model, s.last_activity_at, "
                   "s.message_count, s.chat_type, s.pinned, s.archived "
                   "FROM sessions s")
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY s.pinned DESC, COALESCE(s.last_activity_at, s.started_at) DESC, s.id DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            with db() as conn:
                rows = conn.execute(sql, args).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM sessions s" +
                    (" WHERE " + " AND ".join(where) if where else ""), args[:-2]).fetchone()[0]
                # 无标题会话的首条用户消息补标题：一条查询批量取，
                # 不再逐行开 SQLite 连接（会话多/库繁忙时会拖慢整页列表）
                first_user = {}
                missing = [r["id"] for r in rows if not r["title"]]
                if missing:
                    marks = ",".join("?" * len(missing))
                    for rr in conn.execute(
                            f"SELECT session_id, content FROM messages "
                            f"WHERE role='user' AND session_id IN ({marks}) ORDER BY id",
                            missing):
                        first_user.setdefault(rr[0], rr[1] or "")
            sessions = []
            for r in rows:
                raw_title = r["title"] or first_user.get(r["id"], "")
                title = sanitize_session_title(raw_title)
                sessions.append({
                    "id": r["id"], "title": title or "(无标题)", "source": r["source"],
                    "model": r["model"], "last_activity_at": r["last_activity_at"],
                    "message_count": r["message_count"], "chat_type": r["chat_type"],
                    "pinned": bool(r["pinned"]), "archived": bool(r["archived"]),
                })
            return self._json(200, {"sessions": sessions, "total": total})

        m = re.fullmatch(r"/api/sessions/([^/]+)/messages", path)
        if m:
            sid = m.group(1)
            tail = max(1, min(qi("tail", 100), 500))
            before_s, since_s = q("before"), q("since")
            before = qi("before", None)
            since = qi("since", None)
            if (before_s is not None and before is None) or (since_s is not None and since is None):
                return self._json(400, {"error": "before/since 参数不是数字"})
            with db() as conn:
                sess = conn.execute(
                    "SELECT id, title, source, model, last_activity_at, message_count "
                    "FROM sessions WHERE id=?", (sid,)).fetchone()
                if not sess:
                    return self._json(404, {"error": "会话不存在"})
                if since is not None:
                    rows = conn.execute(
                        "SELECT * FROM messages WHERE session_id=? AND id > ? "
                        "ORDER BY id LIMIT 500", (sid, since)).fetchall()
                elif before is not None:
                    rows = conn.execute(
                        "SELECT * FROM messages WHERE session_id=? AND id < ? "
                        "ORDER BY id DESC LIMIT ?", (sid, before, tail)).fetchall()
                    rows = list(reversed(rows))
                else:
                    rows = conn.execute(
                        "SELECT * FROM messages WHERE session_id=? "
                        "ORDER BY id DESC LIMIT ?", (sid, tail)).fetchall()
                    rows = list(reversed(rows))
            msgs = [row_to_msg(r) for r in rows
                    if (r["display_kind"] or "") not in ("hidden", "internal_notification")]
            return self._json(200, {
                "session": {"id": sess["id"], "title": sess["title"] or "",
                            "source": sess["source"], "model": sess["model"],
                            "last_activity_at": sess["last_activity_at"],
                            "message_count": sess["message_count"]},
                "messages": msgs,
                # 还有更早消息：翻页（before）与首屏（tail 拉满）都算
                "has_more_before": since is None and len(rows) >= tail,
            })

        m = re.fullmatch(r"/api/messages/(\d+)", path)
        if m:
            with db() as conn:
                r = conn.execute("SELECT * FROM messages WHERE id=?", (int(m.group(1)),)).fetchone()
            if not r:
                return self._json(404, {"error": "消息不存在"})
            return self._json(200, {"message": row_to_msg(r, full=True)})

        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._json(404, {"error": "任务不存在"})
            return self._json(200, {
                "id": job["id"], "session_id": job.get("session_id"),
                "status": job["status"], "error": job["error"],
                "started_at": job["started_at"], "ended_at": job["ended_at"],
            })

        if path == "/api/file" or path.startswith("/api/file/"):
            file_param = q("path", "")
            if not file_param and path.startswith("/api/file/"):
                sub = urllib.parse.unquote(path[len("/api/file/"):])
                if sub:
                    file_param = str((UPLOADS_DIR / sub).resolve())
            return self._api_file(file_param, q("download", ""))

        if path == "/api/models":
            return self._api_models()

        return self._json(404, {"error": "接口不存在"})

    def _api_file(self, raw_path: str, download: str = ""):
        raw_path = (raw_path or "").strip()
        # 兼容 file:/// 与 file:// 及 MSYS 风格 /d/... 写法（消息里两种路径都可能出现）
        if raw_path.lower().startswith("file:///"):
            raw_path = urllib.parse.unquote(raw_path[8:])
        elif raw_path.lower().startswith("file://"):
            raw_path = urllib.parse.unquote(raw_path[7:])
        msys = re.fullmatch(r"/([A-Za-z])/(.*)", raw_path.replace("\\", "/"))
        if msys:
            raw_path = msys.group(1).upper() + ":/" + msys.group(2)
        try:
            fp = Path(raw_path).resolve()
            if not fp.is_file():
                return self._json(404, {"error": "文件不存在"})
            # 允许 UPLOADS_DIR 里的任何上传附件预览/下载；如果是系统其他路径，只允许 MEDIA_EXT 且不能在 SENSITIVE_NAME
            is_upload = False
            try:
                fp.relative_to(UPLOADS_DIR.resolve())
                is_upload = True
            except ValueError:
                pass
            if not is_upload:
                if fp.name.lower() in SENSITIVE_NAME or fp.suffix.lower() not in MEDIA_EXT:
                    return self._json(403, {"error": "该文件类型不允许预览"})
            file_size = fp.stat().st_size
        except Exception as exc:
            return self._json(404, {"error": f"读取失败：{exc}"})
        import mimetypes
        FILE_MIMES = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".pdf": "application/pdf",
            ".txt": "text/plain; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
            ".heic": "image/heic",
            ".heif": "image/heif",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mov": "video/quicktime",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".wav": "audio/wav",
            ".flac": "audio/flac",
            ".zip": "application/zip",
        }
        ctype = FILE_MIMES.get(fp.suffix.lower()) or mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
        disp = "attachment" if (download or "").strip() in ("1", "true", "yes") else "inline"
        clean_stem = re.sub(r'[^A-Za-z0-9_.-]', '', fp.stem)[:30] or "document"
        safe_ascii = clean_stem + fp.suffix.lower()

        status, start, end = _parse_byte_range(self.headers.get("Range") or "", file_size)
        if status == 416:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        send_len = 0 if file_size == 0 else (end - start + 1)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(send_len))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f"{disp}; filename=\"{safe_ascii}\"; filename*=UTF-8''{urllib.parse.quote(fp.name)}")
        self.end_headers()
        if getattr(self, "_head", False) or send_len == 0:
            return
        try:
            with open(fp, "rb") as f:
                if start > 0:
                    f.seek(start)
                remaining = send_len
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _api_upload(self, body):
        orig_filename = _s(body.get("filename")).strip() or "upload.bin"
        data_str = _s(body.get("data"))
        if not data_str:
            return self._json(400, {"error": "未提供文件数据"})

        ext = Path(orig_filename).suffix.lower()
        if not ext:
            ext = ".bin"

        is_image = ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

        if "," in data_str:
            data_str = data_str.split(",", 1)[1]

        try:
            raw_bytes = base64.b64decode(data_str)
        except Exception:
            return self._json(400, {"error": "文件 Base64 解析失败"})

        if len(raw_bytes) > 30 * 1024 * 1024:
            return self._json(400, {"error": "文件大小超过 30MB 限制"})

        clean_name = re.sub(r"[^\w\-.]", "_", Path(orig_filename).stem)[:40] or "file"
        save_name = f"{int(time.time())}_{secrets.token_hex(4)}_{clean_name}{ext}"
        save_path = UPLOADS_DIR / save_name
        try:
            save_path.write_bytes(raw_bytes)
        except Exception as exc:
            return self._json(500, {"error": f"保存文件失败：{exc}"})

        log(f"uploaded {'image' if is_image else 'file'}: {save_name} ({len(raw_bytes)} bytes)")
        return self._json(200, {
            "ok": True,
            "filename": orig_filename,
            "saved_name": save_name,
            "is_image": is_image,
            "size": len(raw_bytes),
            "path": str(save_path.resolve()),
            "url": f"/api/file?path={urllib.parse.quote(str(save_path.resolve()))}"
        })

    def _api_send(self, path, body):
        m = re.fullmatch(r"/api/sessions/([^/]+)/send", path)
        if not m:
            return self._json(404, {"error": "接口不存在"})
        sid = m.group(1)
        text = _s(body.get("text")).strip()
        image = _s(body.get("image")).strip() or None
        file_path = _s(body.get("file")).strip() or None
        if not text and not image and not file_path:
            return self._json(400, {"error": "消息、图片或文件不能全部为空"})
        if len(text) > 28000:
            return self._json(400, {"error": "消息过长"})
        if image and not os.path.isfile(image):
            return self._json(400, {"error": "图片文件不存在"})
        if file_path and not os.path.isfile(file_path):
            return self._json(400, {"error": "上传文件不存在"})
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if not exists:
            return self._json(404, {"error": "会话不存在"})
        res = start_job(sid, text, image=image, file=file_path)
        if "error" in res:
            return self._json(429, res)
        return self._json(200, res)

    def _api_rename(self, path, body):
        m = re.fullmatch(r"/api/sessions/([^/]+)/rename", path)
        if not m:
            return self._json(404, {"error": "接口不存在"})
        sid = m.group(1)
        title = _s(body.get("title")).strip()[:80]
        if not title:
            return self._json(400, {"error": "标题为空"})
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if not exists:
            return self._json(404, {"error": "会话不存在"})
        try:
            r = subprocess.run([str(HERMES_BIN), "sessions", "rename", sid, title],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60, creationflags=0x08000000,
                               env={**os.environ, "HERMES_HOME": str(HERMES_HOME)})
            if r.returncode != 0:
                return self._json(500, {"error": (r.stderr or r.stdout or "重命名失败")[-400:]})
        except Exception as exc:
            return self._json(500, {"error": f"重命名失败：{exc}"})
        return self._json(200, {"ok": True})

    def _api_models(self):
        """Hermes 模型清单：只含桌面端蓝色开关勾选的模型；附思考强度档位与当前值。"""
        prefs = read_desktop_prefs()
        out = {"current_model": "", "current_provider": "", "current_effort": "",
               "effort_levels": [{"value": v, "label": l} for v, l in [
                   ("none", "None"), ("minimal", "Minimal"), ("low", "Low"),
                   ("medium", "Medium"), ("high", "High"), ("xhigh", "Extra High"),
                   ("max", "Max"), ("ultra", "Ultra")]],
               "groups": [], "visible_only": False}
        try:
            text = CONFIG_YAML.read_text(encoding="utf-8")
        except OSError:
            text = ""
        m = re.search(r"^model:\s*\n((?:[ ]{2,}\S.*\n)*)", text, re.M)
        if m:
            block = m.group(1)
            dm = re.search(r"^[ ]{2}default:\s*(\S+)", block, re.M)
            pm = re.search(r"^[ ]{2}provider:\s*(\S+)", block, re.M)
            if dm:
                out["current_model"] = dm.group(1).strip("'\"")
            if pm:
                out["current_provider"] = pm.group(1).strip("'\"")
        # 取第一个非空档位（config 里 reasoning_effort 可能出现多次，空值在前时不取错）
        efforts = re.findall(r"reasoning_effort:\s*['\"]?([A-Za-z]*)['\"]?", text)
        cfg_effort = next((e.lower() for e in efforts if e), "")
        out["current_effort"] = prefs.get("effort") or cfg_effort or "max"

        # 勾选集合（桌面蓝色开关）；provider:: 空模型名是“整组隐藏”哨兵，跳过
        visible = [k for k in prefs.get("visible", []) if k and not k.endswith("::")]
        out["visible_only"] = bool(visible)
        groups = {}

        def group(slug):
            return groups.setdefault(slug, {"provider": slug, "name": slug,
                                            "models": [], "_seen": set()})

        def add(slug, model):
            g = group(slug)
            if model and model not in g["_seen"]:
                g["_seen"].add(model)
                g["models"].append(model)

        if visible:
            # 只显示桌面勾选（蓝色打开）的模型
            for key in visible:
                prov, _, model = key.rpartition("::")
                add(prov, model)
        else:
            # 兜底：桌面偏好读不到时用 config providers + 发现缓存（全量）
            pm2 = re.search(r"^providers:\s*\n((?:[ \t]+\S.*\n|(?:[ \t]*\n))*)", text, re.M)
            if pm2:
                cur, in_models = None, False
                for line in pm2.group(1).splitlines():
                    km = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
                    if km:
                        cur, in_models = km.group(1), False
                        group(cur)
                        continue
                    if cur is None:
                        continue
                    if re.match(r"^    models:\s*$", line):
                        in_models = True
                        continue
                    if in_models:
                        em2 = re.match(r"^      ['\"]?([^:'\"]+)['\"]?\s*:", line)
                        if em2:
                            add(cur, em2.group(1).strip())
                            continue
                        in_models = False
                    dm2 = re.match(r"^    model:\s*(\S+)", line)
                    if dm2:
                        add(cur, dm2.group(1).strip("'\""))
            try:
                cache = json.loads((HERMES_HOME / "provider_models_cache.json")
                                   .read_text(encoding="utf-8"))
                for slug, data in cache.items():
                    for model in (data.get("models") or []):
                        add(slug, model)
            except Exception:
                pass

        # 提供方显示名（模仿桌面模型对话框的分组标题）
        builtin = {"xiaomi": "Xiaomi MiMo", "alibaba": "Alibaba", "copilot": "GitHub Copilot",
                   "ollama-cloud": "Ollama Cloud", "opencode-free": "OpenCode Free",
                   "nvidia": "NVIDIA NIM", "nvidia-build": "NVIDIA Build",
                   "sensenova": "商汤 Sensenova", "openai": "DeepSeek 本地代理",
                   "openai-api": "OpenAI API", "moa": "MoA", "custom": "自定义",
                   "alibaba-cn": "Alibaba CN", "alibaba-coding-plan": "Alibaba 编码套餐"}
        names = {}
        pm3 = re.search(r"^providers:\s*\n((?:[ \t]+\S.*\n|(?:[ \t]*\n))*)", text, re.M)
        if pm3:
            cur = None
            for line in pm3.group(1).splitlines():
                km = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
                if km:
                    cur = km.group(1)
                nm = re.match(r"^    name:\s*(.+?)\s*$", line)
                if nm and cur:
                    names[cur] = nm.group(1).strip("'\"")

        out["groups"] = []
        for slug, g in groups.items():
            models = []
            for model in g["models"]:
                models.append({
                    "key": f"{slug}::{model}",
                    "model": model,
                    "label": model_display_name(model),
                })
            if not models:
                continue
            out["groups"].append({
                "provider": slug,
                "name": names.get(slug) or builtin.get(slug) or g["name"],
                "models": models,
            })
        return self._json(200, out)

    def _api_switch_model(self, path, body):
        """切换会话模型：走 hermes 的 /model 命令（与桌面端 /model 同一机制）。"""
        m = re.fullmatch(r"/api/sessions/([^/]+)/model", path)
        if not m:
            return self._json(404, {"error": "接口不存在"})
        sid = m.group(1)
        model = _s(body.get("model")).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
            return self._json(400, {"error": "模型名不合法"})
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if not exists:
            return self._json(404, {"error": "会话不存在"})
        res = start_job(sid, f"/model {model}")
        if "error" in res:
            return self._json(429, res)
        return self._json(200, res)

    def _api_switch_effort(self, path, body):
        """切换思考强度：走 hermes 的 /reasoning 命令（与桌面端强度选择同一机制）。"""
        m = re.fullmatch(r"/api/sessions/([^/]+)/reasoning", path)
        if not m:
            return self._json(404, {"error": "接口不存在"})
        sid = m.group(1)
        level = _s(body.get("effort")).strip().lower()
        if level not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            return self._json(400, {"error": "强度档位不合法"})
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if not exists:
            return self._json(404, {"error": "会话不存在"})
        res = start_job(sid, f"/reasoning {level}")
        if "error" in res:
            return self._json(429, res)
        return self._json(200, res)

    def _api_new_conversation(self, body):
        text = _s(body.get("text")).strip()
        image = _s(body.get("image")).strip() or None
        file_path = _s(body.get("file")).strip() or None
        model = _s(body.get("model")).strip() or None
        effort = _s(body.get("effort")).strip().lower() or None
        if not text and not image and not file_path:
            return self._json(400, {"error": "消息、图片或文件不能全部为空"})
        if image and not os.path.isfile(image):
            return self._json(400, {"error": "图片文件不存在"})
        if file_path and not os.path.isfile(file_path):
            return self._json(400, {"error": "上传文件不存在"})
        if model and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
            return self._json(400, {"error": "模型名不合法"})
        if effort and effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            return self._json(400, {"error": "强度档位不合法"})
        res = start_job(None, text, discover_new=True, model=model, effort=effort, image=image, file=file_path)
        if "error" in res:
            return self._json(429, res)
        return self._json(200, res)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _is_python_pid(pid: int) -> bool:
    try:
        chk = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, creationflags=0x08000000)
        return "python" in (chk.stdout or "").lower()
    except Exception:
        return False


def _find_pid_by_port(port: int):
    try:
        res = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=0x08000000)
        for line in res.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                return int(parts[-1])
    except Exception:
        pass
    return None


def main():
    import sys
    if "--stop" in sys.argv:
        try:
            pid = None
            if PID_FILE.exists():
                try:
                    pid = int(PID_FILE.read_text().strip())
                except ValueError:
                    pid = None
            if not pid or not _is_python_pid(pid):
                port_pid = _find_pid_by_port(PORT)
                if port_pid and _is_python_pid(port_pid):
                    pid = port_pid
            if not pid or not _is_python_pid(pid):
                print("未找到正在运行的 Hermes 服务进程")
                return
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, creationflags=0x08000000)
            print(f"已停止 PID {pid}" if r.returncode == 0 else f"停止失败：{r.stderr.decode('gbk', 'replace')}")
            try:
                PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass
        except Exception as exc:
            print(f"停止失败：{exc}")
        return
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(LOG_FILE.suffix + ".1"))
    except OSError:
        pass
    try:
        with ThreadingHTTPServer((BIND, PORT), Handler) as httpd:
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
            log(f"server start on {BIND}:{PORT} pid={os.getpid()}")
            threading.Thread(target=_cleanup_loop, daemon=True).start()
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
    except OSError as exc:
        # 端口被占用 = 已有实例在跑（如开机自启与手动启动撞车），静默退出
        log(f"server start failed: {exc}")
        return
    log("server stop")


if __name__ == "__main__":
    main()
