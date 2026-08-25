#!/usr/bin/env python3
"""
Chrome启动器 - 自动配置关键设置
跨平台支持 (Windows / Linux)
自动检查和应用用户期望的Chrome配置
"""

import os
import json
import subprocess
import sys
import time
from pathlib import Path

# 从 config 导入跨平台配置
try:
    from config import CHROME_DEBUG_PORT, CHROME_PATH, CHROME_USER_DATA_DIR, IS_WINDOWS
except ImportError:
    IS_WINDOWS = sys.platform == "win32"
    CHROME_PATH = "chrome.exe" if IS_WINDOWS else "google-chrome"
    CHROME_USER_DATA_DIR = (
        str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data")
        if IS_WINDOWS
        else str(Path.home() / ".config" / "google-chrome")
    )
    CHROME_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", 9222))


def ensure_chrome_preferences(user_data_dir: str):
    """
    确保Chrome有正确的设置
    """
    preferences_path = Path(user_data_dir) / "Default" / "Preferences"

    # 关键设置
    settings_to_apply = {
        # PDF 处理设置
        "plugins": {
            "always_open_pdf_externally": True,  # PDF 下载而不是预览（最重要）
        },
        "download": {
            # 下载位置
            "default_directory": str(Path.home() / "Downloads"),
            # 下载前弹窗
            "prompt_for_download": False,
            # 目录升级标记
            "directory_upgrade": True,
        },
        "browser": {
            "check_default_browser": False,
        },
        # 禁用Chrome PDF查看器
        "profile": {
            "content_settings": {
                "pattern_pairs": {
                    "https://,*": {
                        "plugins": 2  # 2 = Ask, 3 = Block
                    }
                }
            },
            # 防止在关闭所有标签页时关闭窗口
            "exit_type": "Normal",
        }
    }

    # 读取现有设置
    if preferences_path.exists():
        try:
            with open(preferences_path, 'r', encoding='utf-8') as f:
                prefs = json.load(f)
        except:
            prefs = {}
    else:
        prefs = {}

    # 应用新设置
    def deep_update(d, u):
        """递归更新字典"""
        for k, v in u.items():
            if isinstance(v, dict):
                d[k] = deep_update(d.get(k, {}), v)
            else:
                d[k] = v
        return d

    prefs = deep_update(prefs, settings_to_apply)

    # 写回设置
    preferences_path.parent.mkdir(parents=True, exist_ok=True)
    with open(preferences_path, 'w', encoding='utf-8') as f:
        json.dump(prefs, f, indent=2)

    print(f"✓ Chrome 设置已配置:")
    print(f"  - PDF 处理: 默认下载（always_open_pdf_externally=True）")
    print(f"  - 下载目录: {settings_to_apply['download']['default_directory']}")
    print(f"  - 下载提示: 关闭")
    print(f"  - 窗口保持: 关闭所有标签页后保留窗口")


def kill_chrome():
    """Kill all Chrome processes (cross-platform)."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                       capture_output=True)
        print("✓ Chrome processes killed")
    else:
        os.system("pkill -9 chrome")
        print("✓ Chrome processes killed")


def launch_chrome(
    use_user_config: bool = False,
    headless: bool = False,
    return_details: bool = False,
):
    """
    启动Chrome，配合最优设置

    Args:
        use_user_config: 使用用户的真实配置目录（更稳定）
        headless: 无头模式
    """

    if use_user_config:
        # 使用用户的真实Chrome配置
        user_data_dir = CHROME_USER_DATA_DIR
        print(f"使用用户配置: {user_data_dir}")
    else:
        # 创建临时目录
        import tempfile
        profile_root = os.environ.get("CHROME_PROFILE_ROOT")
        if profile_root:
            Path(profile_root).expanduser().mkdir(parents=True, exist_ok=True)
            profile_root = str(Path(profile_root).expanduser().resolve())
        user_data_dir = tempfile.mkdtemp(
            prefix=f"chrome_{CHROME_DEBUG_PORT}_",
            dir=profile_root,
        )
        print(f"创建临时配置: {user_data_dir}")

    # 应用设置
    ensure_chrome_preferences(user_data_dir)

    # 构建启动命令
    chrome_args = [
        CHROME_PATH,
        f"--remote-debugging-port={CHROME_DEBUG_PORT}",
        f"--user-data-dir={user_data_dir}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-extensions",
        "--disable-gpu",
        "--disable-background-networking",
        "--disk-cache-size=67108864",
        "--media-cache-size=16777216",
        "--disable-application-cache",
    ]

    if headless:
        chrome_args.append("--headless=new")

    # 启动Chrome
    print(f"正在启动 Chrome ({CHROME_PATH})...")
    extra_kwargs = {}
    if not IS_WINDOWS:
        extra_kwargs["preexec_fn"] = os.setsid
    else:
        extra_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **extra_kwargs
    )

    print(f"✓ Chrome 已启动 (PID: {proc.pid})")
    print(f"✓ 远程调试端口: {CHROME_DEBUG_PORT}")

    # 等待启动
    time.sleep(6)

    # 验证连接
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex(('127.0.0.1', CHROME_DEBUG_PORT))
        sock.close()

        if result == 0:
            print("✓ Chrome CDP 端口已就绪")
            return (proc, user_data_dir, not use_user_config) if return_details else proc
        else:
            print("⚠️  CDP 端口未响应，但Chrome已启动")
            return (proc, user_data_dir, not use_user_config) if return_details else proc
    except Exception as e:
        print(f"⚠️  连接检查失败: {e}")
        return (proc, user_data_dir, not use_user_config) if return_details else proc


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="启动配置好的Chrome")
    parser.add_argument("--user-config", action="store_true", help="使用用户真实配置目录")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--kill", action="store_true", help="关闭所有Chrome进程")

    args = parser.parse_args()

    if args.kill:
        kill_chrome()
        sys.exit(0)

    proc = launch_chrome(use_user_config=args.user_config, headless=args.headless)
