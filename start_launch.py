#!/usr/bin/env python3
"""启动 launch.sh 并将输出写入日志文件。"""
import subprocess
import os
import sys
import time

log_path = "/home/coze/Download_paper/run18.log"
work_dir = "/home/coze/Download_paper"

os.environ["DISPLAY"] = ":99"
os.environ["DP_PAGE_LOAD_TIMEOUT"] = "120"
os.environ["DP_CLOUDFLARE_TIMEOUT"] = "600"

log_file = open(log_path, "w")

proc = subprocess.Popen(
    ["bash", "examples/launch.sh"],
    stdout=log_file,
    stderr=subprocess.STDOUT,
    cwd=work_dir,
)

print(f"PID={proc.pid}")
print(f"log={log_path}")

# 等 8 秒看一下状态
time.sleep(8)

# 检查进程是否还在
try:
    os.kill(proc.pid, 0)
    print(f"Process {proc.pid} is still running")
except OSError:
    print(f"Process {proc.pid} has exited")
    ret = proc.poll()
    print(f"Exit code: {ret}")

# 打印日志前 80 行
log_file.flush()
with open(log_path, "r") as f:
    lines = f.readlines()
    print(f"\n=== Log ({len(lines)} lines) ===")
    for line in lines[:80]:
        print(line.rstrip())
