# -*- coding: utf-8 -*-
"""Hermes 远程网站看门狗：每 30 秒探活 /api/health，服务死了自动重启。
独立于服务进程运行（pythonw 隐藏窗口）。
"""
import sys
import time
import subprocess
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 动态探测 Python 执行环境
PYE = sys.executable
if PYE.lower().endswith("python.exe"):
    pyw_cand = Path(PYE).with_name("pythonw.exe")
    PYW = str(pyw_cand) if pyw_cand.exists() else PYE
else:
    PYW = PYE

SERVER = HERE / "server.py"
LOG = HERE / "watchdog.log"
HEALTH = "http://127.0.0.1:9220/api/health"


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


log("watchdog start")
while True:
    try:
        if not healthy():
            time.sleep(2)
            if not healthy():
                log("服务无响应，开始重启")
                try:
                    subprocess.run([PYE, str(SERVER), "--stop"], cwd=str(HERE),
                                   timeout=20, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=0x08000000)
                except Exception as exc:
                    log(f"stop 异常（忽略继续）：{exc}")
                time.sleep(1)
                # DETACHED_PROCESS | CREATE_NO_WINDOW，不继承任何句柄
                subprocess.Popen([PYW, str(SERVER)], cwd=str(HERE), close_fds=True,
                                 creationflags=0x08000008)
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
