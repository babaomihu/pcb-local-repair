# SPDX-License-Identifier: GPL-3.0-or-later
"""Disk repair entry; optional read-only editor selection hints live in the UI."""
import pcbnew
import wx

_frame = None


class LocalRepairAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "PCB Local：修复磁盘文件副本"
        self.category = "PCB Local Repair"
        self.description = "修复已保存磁盘副本并独立验收；可只读当前选择帮助勾选连接"
        self.show_toolbar_button = False

    def Run(self):
        global _frame
        try:
            if str(pcbnew.Version()) != "10.0.4":
                raise RuntimeError("此入口仅验证过 KiCad 10.0.4")
            # wx keeps the Python wrapper after the C++ window is destroyed.
            if _frame is not None and bool(_frame) and not _frame.IsBeingDeleted():
                _frame.Show()
                _frame.Raise()
                return
            from .dialog import LauncherFrame
            _frame = LauncherFrame()
            _frame.Show()
        except Exception as exc:
            wx.MessageBox(str(exc), "PCB Local 启动失败", wx.OK | wx.ICON_ERROR)
