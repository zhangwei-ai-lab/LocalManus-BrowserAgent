"""Windows 进程卫生：识别并回收 browser_use 启动的 Chromium 子进程。

为什么需要这个东西
------------------
browser_use 启动的链路是：

    python (本进程) → node.exe (playwright driver) → chrome.exe × N (renderer/gpu/utility)

Windows 没有 POSIX 进程组的概念，**父进程退出不会级联杀子进程**。只要 Python
这一侧异常退出、被 Ctrl+C 打断、或者在任务管理器里被结束，上面那串 chrome.exe
就会脱离父进程继续常驻。更糟的是 browser_use 的默认启动参数里有：

    --no-startup-window          不弹窗，用户看不见
    --disable-background-timer-throttling
    --disable-renderer-backgrounding
    --disable-backgrounding-occluded-windows

也就是说它**既不显示窗口，也不进入后台节流**，会一直按全速空转。用户能感知到
的只有「风扇响了 / 内存少了一截 / 电池掉得快」，很难联想到是某个 Agent 进程
留下的浏览器。

本模块做两件事：

1. ``list_browser_processes()``：列出所有由 Playwright 缓存目录（``ms-playwright``）
   启动的 chrome / headless_shell 进程，带父子关系；
2. ``BrowserProcessGuard``：在浏览器启动前拍一张 PID 快照，清理时只杀
   **本进程的子孙进程**（不误伤用户自己的 Chrome，也不误伤并发的另一个 Agent 任务）。

非 Windows 平台一律返回空列表、不做任何动作（macOS/Linux 一般不会出现这个
问题，父进程退出时子进程会收到 SIGHUP）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set

# Playwright 把浏览器内核下载到这里，用它区分「Agent 启动的浏览器」和「用户自己的 Chrome」
BROWSER_PATH_MARKER = "ms-playwright"

# 可能出现的进程名（不同平台/内核版本命名不同）
BROWSER_EXE_NAMES = {
    "chrome.exe",
    "chromium.exe",
    "headless_shell.exe",
    "chrome",
    "chromium",
    "headless_shell",
}

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 1024


@dataclass(frozen=True)
class ProcInfo:
    """一条进程记录。"""

    pid: int
    ppid: int
    name: str
    path: str

    @property
    def is_playwright_browser(self) -> bool:
        """是不是 Playwright 缓存目录下的浏览器进程。"""
        return (
            BROWSER_PATH_MARKER in (self.path or "").lower()
            and (self.name or "").lower() in BROWSER_EXE_NAMES
        )


if os.name == "nt":  # pragma: no cover - 仅在 Windows 上编译
    from ctypes import wintypes

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _process_path(pid: int) -> str:
    """取进程的可执行文件完整路径，取不到返回空串（权限不足等）。"""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(_MAX_PATH)
        size = wintypes.DWORD(_MAX_PATH)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def list_processes() -> List[ProcInfo]:
    """枚举当前所有进程（pid / ppid / 名称 / 路径）。非 Windows 返回空列表。"""
    if os.name != "nt":
        return []

    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return []

    entries: List[ProcInfo] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            name = entry.szExeFile or ""
            pid = int(entry.th32ProcessID)
            if pid:
                entries.append(
                    ProcInfo(
                        pid=pid,
                        ppid=int(entry.th32ParentProcessID),
                        name=name,
                        path=_process_path(pid),
                    )
                )
            if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(snapshot)
    return entries


def list_browser_processes() -> List[ProcInfo]:
    """只保留 Playwright 缓存目录下的浏览器进程。"""
    return [p for p in list_processes() if p.is_playwright_browser]


def _descendants(root_pid: int, processes: Sequence[ProcInfo]) -> Set[int]:
    """计算 root_pid 的全部子孙进程 PID（按父子关系做一次 BFS）。"""
    children: dict[int, List[int]] = {}
    for proc in processes:
        children.setdefault(proc.ppid, []).append(proc.pid)

    found: Set[int] = set()
    queue = [root_pid]
    while queue:
        current = queue.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                queue.append(child)
    return found


def kill_process_tree(pid: int, dry_run: bool = False) -> bool:
    """强制结束进程及其子进程（Windows 用 taskkill /T /F）。"""
    if dry_run:
        return True
    try:
        # 注意：taskkill 输出是 GBK 编码，用 text=True 会抛 UnicodeDecodeError，
        # 所以只取退出码，不做解码。
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class BrowserProcessGuard:
    """浏览器进程守卫：启动前拍快照，清理时只回收本次启动的浏览器。"""

    def __init__(self, root_pid: Optional[int] = None) -> None:
        self.root_pid = root_pid or os.getpid()
        # 启动前已经存在的浏览器进程，清理时一概不碰（可能是用户自己的，或别的任务的）
        self._before: Set[int] = {p.pid for p in list_browser_processes()}

    def ours(self) -> List[ProcInfo]:
        """本次启动产生、且仍是本进程子孙的浏览器进程。"""
        processes = list_processes()
        tree = _descendants(self.root_pid, processes)
        return [
            p
            for p in processes
            if p.is_playwright_browser and p.pid not in self._before and p.pid in tree
        ]

    def sweep(self, dry_run: bool = False) -> List[int]:
        """回收本次启动的浏览器进程，返回被结束的 PID 列表。

        只处理「本进程的子孙 + 启动后新出现」的进程，因此不会误杀：

        * 用户自己打开的 Chrome（路径不在 ms-playwright 下）；
        * 并发运行的另一个 Agent 任务（不是本进程的子孙）。
        """
        targets = self.ours()
        killed: List[int] = []
        for proc in targets:
            if kill_process_tree(proc.pid, dry_run=dry_run):
                killed.append(proc.pid)
        return killed


class OrphanScanner:
    """查找「父进程已经不在」的 Playwright 浏览器——典型的历史残留。"""

    def __init__(self) -> None:
        self._processes = list_processes()
        self._alive = {p.pid for p in self._processes}

    def orphans(self) -> List[ProcInfo]:
        return [
            p
            for p in self._processes
            if p.is_playwright_browser and p.ppid not in self._alive
        ]

    def kill_all_playwright_browsers(self, dry_run: bool = False) -> List[int]:
        """清理**所有** Playwright 浏览器进程（含正在跑的），供手动救援使用。"""
        killed: List[int] = []
        for proc in self._processes:
            if proc.is_playwright_browser and kill_process_tree(proc.pid, dry_run=dry_run):
                killed.append(proc.pid)
        return killed


def iter_browser_processes() -> Iterable[ProcInfo]:
    """便捷迭代器（给脚本用）。"""
    yield from list_browser_processes()
