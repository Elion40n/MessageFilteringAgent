"""Windows execution-state request that keeps the system awake while active.

程序运行期间请求阻止系统自动睡眠；该请求允许屏幕按系统设置熄灭。
"""

from __future__ import annotations

import ctypes
import os


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """Manage one start/stop lifecycle for the Windows execution request.

    非 Windows 平台不调用系统 API，并明确返回未启用状态。
    """

    def __init__(self) -> None:
        self.active = False

    def start(self) -> bool:
        """Request system-required execution; return False off Windows."""
        if os.name != "nt":
            return False
        # ES_CONTINUOUS keeps the request active until stop() restores the default state.
        # ES_CONTINUOUS 让请求持续有效，直到 stop() 显式恢复系统默认状态。
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        if not result:
            raise OSError("Windows refused the keep-awake execution-state request")
        self.active = True
        return True

    def stop(self) -> None:
        """Release the keep-awake request exactly once."""
        if self.active:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.active = False