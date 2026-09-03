"""
Download_paper 项目配置文件

存储默认配置，所有脚本都使用这个文件中的配置。
支持跨平台 (Linux / Windows) 自动检测 Chrome 路径。
"""

import os
import platform
import sys
from pathlib import Path


# ============================================================================
# 平台检测
# ============================================================================

IS_WINDOWS = platform.system() == 'Windows'


def _detect_chrome_path() -> str:
    """Auto-detect Chrome executable path across Windows and Linux."""
    env_path = os.environ.get('CHROME_PATH', '').strip()
    if env_path:
        env_path = os.path.expanduser(os.path.expandvars(env_path))
        if os.path.isfile(env_path):
            return env_path
        print(f"⚠ CHROME_PATH does not point to a file: {env_path}", file=sys.stderr)

    if IS_WINDOWS:
        candidates = []
        program_files_dirs = [
            os.environ.get('ProgramFiles', 'C:\\Program Files'),
            os.environ.get('ProgramFiles(x86)', 'C:\\Program Files (x86)'),
            os.environ.get('LOCALAPPDATA', ''),
        ]
        for base in program_files_dirs:
            if not base:
                continue
            candidates.append(os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'))
        candidates.append('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe')
    else:
        candidates = [
            '/opt/google/chrome/chrome',
            '/usr/bin/google-chrome',
            '/usr/bin/chromium-browser',
            '/usr/bin/chromium',
            '/snap/bin/chromium',
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    # Fallback
    return 'google-chrome' if not IS_WINDOWS else 'chrome.exe'


def _detect_chrome_user_data_dir() -> str:
    """Auto-detect Chrome user data directory across platforms."""
    # 优先使用环境变量覆盖（脚本以非 profile 所有者运行时很有用）
    env_dir = os.environ.get('CHROME_USER_DATA_DIR', '')
    if env_dir:
        return env_dir
    if IS_WINDOWS:
        local_appdata = os.environ.get('LOCALAPPDATA', '')
        if local_appdata:
            return os.path.join(local_appdata, 'Google', 'Chrome', 'User Data')
        return str(Path.home() / 'AppData' / 'Local' / 'Google' / 'Chrome' / 'User Data')
    else:
        return str(Path.home() / '.config' / 'google-chrome')


# ============================================================================
# Chrome 配置（持久化登录状态）
# ============================================================================

# Chrome 可执行文件路径（自动检测）
CHROME_PATH = _detect_chrome_path()

# Chrome用户数据目录（包含所有cookies、登录状态、历史记录等）
CHROME_USER_DATA_DIR = _detect_chrome_user_data_dir()

# Chrome Default Profile（如果使用特定profile）
CHROME_PROFILE = os.environ.get("CHROME_PROFILE", "Default")

# Chrome远程调试端口（如果使用remote debugging）
CHROME_DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", 9222))

# ============================================================================
# 输出目录配置
# ============================================================================

# 捕获数据保存目录
CAPTURED_DATA_DIR = os.environ.get("CAPTURED_DATA_DIR", "captured_data")

# Markdown输出目录
MARKDOWN_OUTPUT_DIR = "markdown_output"

# 默认输出目录（相对于脚本位置）
OUTPUT_DIR_DEFAULT = os.environ.get("OUTPUT_DIR_DEFAULT", str(Path(__file__).resolve().parent / CAPTURED_DATA_DIR))

# ============================================================================
# 输出格式配置
# ============================================================================

# 是否启用详细日志
VERBOSE = True

# 保存原始HTML文档
SAVE_RAW_HTML = True

# 保存渲染后的HTML
SAVE_RENDERED_HTML = True

# 当参考文献为空时是否仍然保存Markdown
# 注意：即使没有参考文献，通常也应该保存metadata和markdown
# 只有特殊情况（如标题为空）时才应该跳过保存
SAVE_WITHOUT_REFERENCES = True

# ============================================================================
# 批处理配置
# ============================================================================

# 批次处理时是否启用随机休眠（防止被封锁）
BATCH_SLEEP_ENABLED = True

# 批次处理随机休眠最小/最大秒数
BATCH_SLEEP_MIN = int(os.environ.get("BATCH_SLEEP_MIN", 30))
BATCH_SLEEP_MAX = int(os.environ.get("BATCH_SLEEP_MAX", 60))

# ============================================================================
# 脚本配置
# ============================================================================

# 脚本是否使用有头浏览器
HEADLESS = os.environ.get("HEADLESS", "false").lower() in ("1", "true", "yes", "on")

# 默认使用持久化Chrome还是remote debugging
# 可选值: "persistent" 或 "remote"
USE_CHROME_MODE = os.environ.get("USE_CHROME_MODE", "persistent")  # 使用持久化用户数据目录

print("✓ 配置已加载")
print(f"  - Chrome: {CHROME_PATH}")
print(f"  - Chrome用户数据目录: {CHROME_USER_DATA_DIR}")
print(f"  - Chrome模式: {USE_CHROME_MODE}")
print(f"  - 平台: {'Windows' if IS_WINDOWS else 'Linux'}")
