# -*- coding: utf-8 -*-
"""Hermes 远程网站看门狗：每 30 秒探活 /api/health，服务异常退出时自动重启。
支持单实例防重、watchdog.pid 状态管理及 --stop 优雅退出，全程隐藏窗口静默运行。
"""
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 动态探测 Python 执行环境（兼容无 python.exe 仅含 pythonw.exe 的轻量化 venv）
PYE = sys.executable
if PYE.lower().endswith("python.exe"):
    pyw_cand = Path(PYE).with_name("pythonw.exe")
    PYW = str(pyw_cand) if pyw_cand.exists() else PYE
else:
    PYW = PYE

STOP_PY = PYE if Path(PYE).exists() else PYW
SERVER = HERE / "server.py"
LOG = HERE / "watchdog.log"
PID_FILE = HERE / "watchdog.pid"


def _resolve_port() -> int:
    port_s = os.environ.get("PORT", "")
    env_file = HERE / ".env"
    if not port_s and env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("PORT="):
                    port_s = line.split("=", 1)[1].strip().strip("'\"")
                    break
        except OSError:
            pass
    try:
        return int(port_s or "9220")
    except ValueError:
        return 9220


HEALTH = f"http://127.0.0.1:{_resolve_port()}/api/health"
WIN_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WIN_DETACHED = (0x00000008 | WIN_NO_WINDOW) if os.name == "nt" else 0


def _is_python_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, creationflags=WIN_NO_WINDOW)
        return "python" in (r.stdout or "").lower()
    except Exception:
        return False


def _kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                       capture_output=True, creationflags=WIN_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


if "--stop" in sys.argv:
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if _is_python_pid(pid):
                _kill_pid(pid)
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    sys.exit(0)

# 单实例防重检查：已有存活的看门狗时直接退出
try:
    if PID_FILE.exists():
        old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        if old_pid != os.getpid() and _is_python_pid(old_pid):
            sys.exit(0)
except Exception:
    pass


def log(msg: str) -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > 512 * 1024:
            LOG.replace(LOG.with_suffix(".log.1"))
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")
    except OSError:
        pass


def healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


try:
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
except OSError:
    pass
log(f"watchdog start pid={os.getpid()} health={HEALTH}")
while True:
    try:
        if not healthy():
            time.sleep(2)
            if not healthy():
                log("服务无响应，开始重启")
                try:
                    subprocess.run([STOP_PY, str(SERVER), "--stop"], cwd=str(HERE),
                                   timeout=20, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=WIN_NO_WINDOW)
                except Exception as exc:
                    log(f"stop 异常（忽略继续）：{exc}")
                time.sleep(1)
                subprocess.Popen([PYW, str(SERVER)], cwd=str(HERE), close_fds=True,
                                 creationflags=WIN_DETACHED)
                for _ in range(15):
                    time.sleep(2)
                    if healthy():
                        log("重启成功")
                        break
                else:
                    log("重启后 30 秒内仍未恢复（下轮继续）")
    except Exception as exc:
        log(f"循环异常：{exc}")
    time.sleep(30)
