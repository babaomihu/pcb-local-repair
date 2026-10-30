# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeless disk-file repair launcher with explicit read-only selection hints."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

import wx

from . import process as bridge


class LauncherFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="PCB Local · 磁盘文件局部修复", size=(850, 830))
        self.SetMinSize((820, 790))
        self.config = bridge.config_read(Path(__file__).with_name("config.json"))
        self.events = queue.Queue()
        self.task = None
        self.starting = False
        self.control_pending = False
        self.finished = True
        self.close_requested = False
        self.cancel_requested = threading.Event()
        self.cancel_acknowledged = False
        self.last_poll = 0.
        self.inspected = None
        self.inspected_path = None
        self.inspected_selection = None
        self.targets = []
        self.advanced_options = None
        self.reviewed_recommendations = []
        self.recommendation_mode = "shove"
        self.refill_zones = []
        self.refill_target_ids = []
        panel = wx.ScrolledWindow(self, style=wx.VSCROLL)
        panel.SetScrollRate(0, 10)
        layout = wx.BoxSizer(wx.VERTICAL)
        notice = wx.StaticText(panel, label="仅处理您主动选择的磁盘版本。当前编辑器未保存的修改不参与。\n"
                              "可只读当前选择来勾选磁盘连接，不修改编辑器中的板。只有明确采纳并通过检查后才替换磁盘原板。")
        notice.SetForegroundColour(wx.Colour(160, 55, 0))
        layout.Add(notice, 0, wx.ALL | wx.EXPAND, 12)
        self.board = wx.FilePickerCtrl(panel, message="选择已保存的磁盘板",
            wildcard="KiCad PCB (*.kicad_pcb)|*.kicad_pcb", style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL)
        layout.Add(self.board, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        self.import_legacy = wx.Button(panel, label="旧格式安全导入（生成独立工程，原板不变）")
        layout.Add(self.import_legacy, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.import_legacy.Bind(wx.EVT_BUTTON, lambda event: self.start('import-legacy'))
        self.auto_region = wx.CheckBox(panel, label="自动确定局部范围（推荐，无需填写坐标）")
        self.auto_region.SetValue(True)
        layout.Add(self.auto_region, 0, wx.ALL, 12)
        self.roi_label = wx.StaticText(panel, label="高级手工范围：板文件绝对坐标（mm）")
        layout.Add(self.roi_label, 0, wx.LEFT | wx.RIGHT, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.roi_row = row
        self.roi = []
        for label in ("xmin", "ymin", "xmax", "ymax"):
            row.Add(wx.StaticText(panel, label=label), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
            field = wx.TextCtrl(panel, value="", size=(90, -1))
            row.Add(field, 1, wx.RIGHT, 12)
            self.roi.append(field)
        layout.Add(row, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        layer_row = wx.BoxSizer(wx.HORIZONTAL)
        layer_row.Add(wx.StaticText(panel, label="铜层（默认正面 F.Cu）"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.layer = wx.Choice(panel, choices=["F.Cu", "B.Cu"] + ["In%d.Cu" % i for i in range(1, 31)])
        self.layer.SetSelection(0)
        layer_row.Add(self.layer, 0)
        layout.Add(layer_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.inspect = wx.Button(panel, label="1. 检查磁盘板并列出目标")
        layout.Add(self.inspect, 0, wx.ALL, 12)
        self.identity = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 70))
        layout.Add(self.identity, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        self.mode = wx.Choice(panel, choices=["勾选检查发现的连接目标（可多选）", "输入完整网络名：连接 ROI 内全部合格组件"])
        self.mode.SetSelection(0)
        layout.Add(self.mode, 0, wx.ALL | wx.EXPAND, 12)
        self.target = wx.CheckListBox(panel, choices=[], size=(-1, 110))
        layout.Add(self.target, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        self.from_selection = wx.Button(panel, label="用当前选择勾选已检查磁盘工程的连接")
        self.from_selection.Bind(wx.EVT_BUTTON, self.select_from_editor)
        layout.Add(self.from_selection, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)
        self.region_summary = wx.StaticText(panel, label="先检查，再选择连接；自动范围固定为两端周围各 2 mm，不会在失败时扩大。")
        self.region_summary.Wrap(790)
        layout.Add(self.region_summary, 0, wx.ALL | wx.EXPAND, 12)
        self.net = wx.TextCtrl(panel)
        self.net.SetHint("完整网络名（不使用通配符），例如 /Sheet/DATA[0]")
        layout.Add(self.net, 0, wx.ALL | wx.EXPAND, 12)
        self.advanced = wx.Button(panel, label="高级权限：默认仅补线，不改旧线、不打孔")
        self.advanced.Bind(wx.EVT_BUTTON, self.configure_advanced)
        layout.Add(self.advanced, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        self.recommend_button = wx.Button(panel,label="查看并确认局部修改建议（默认不授权）")
        self.recommend_button.Bind(wx.EVT_BUTTON,self.review_recommendations)
        layout.Add(self.recommend_button,0,wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND,12)
        self.refill_button = wx.Button(panel,label="局部重填铜：默认不授权（选择铺铜对象）")
        self.refill_button.Bind(wx.EVT_BUTTON,self.configure_refill)
        layout.Add(self.refill_button,0,wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND,12)
        self.order_optimized = wx.CheckBox(panel,label="按当前共享通道动态协调顺序（不增加尝试或修改权限）")
        layout.Add(self.order_optimized,0,wx.LEFT | wx.RIGHT | wx.BOTTOM,12)
        self.joint_routes = wx.CheckBox(panel,label="2–4 条连接联合修复：协调冲突次序与跨策略候选（共享原预算；优先于顺序协调）")
        layout.Add(self.joint_routes,0,wx.LEFT | wx.RIGHT | wx.BOTTOM,12)
        self.ordinary = wx.CheckBox(panel, label="我确认所选连接及允许重布的网络均为普通单端信号，不是电源/地、差分或要求调长的网络")
        layout.Add(self.ordinary, 0, wx.LEFT | wx.RIGHT, 12)
        actions = wx.FlexGridSizer(4, 2, 8, 8)
        self.repair = wx.Button(panel, label="2. 顺序修复勾选的连接")
        self.cancel = wx.Button(panel, label="取消任务")
        self.report = wx.Button(panel, label="查看报告 / 启动日志")
        self.comparison = wx.Button(panel,label="查看修复前后对比 / 定位对象")
        self.comparison.Bind(wx.EVT_BUTTON,self.show_comparison)
        self.open = wx.Button(panel, label="3. 打开已验收候选（独立窗口）")
        self.save_as = wx.Button(panel, label="一键另存修复结果")
        self.adopt = wx.Button(panel, label="一键采纳到原工程")
        for button in (self.repair, self.cancel, self.report, self.open, self.save_as, self.adopt, self.comparison):
            actions.Add(button, 0, wx.EXPAND)
        layout.Add(actions, 0, wx.ALL, 12)
        self.status = wx.TextCtrl(panel, value="请主动选择磁盘板，然后检查并选择连接。默认无需坐标、模型或 API key。",
                                 style=wx.TE_MULTILINE | wx.TE_READONLY)
        layout.Add(self.status, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 12)
        panel.SetSizer(layout)
        self.inputs = [self.import_legacy, self.board, self.auto_region, *self.roi, self.layer, self.mode, self.target, self.net, self.ordinary, self.recommend_button, self.refill_button, self.order_optimized, self.joint_routes]
        self.auto_region.Bind(wx.EVT_CHECKBOX, self.scope_changed)
        self.target.Bind(wx.EVT_CHECKLISTBOX, self.describe_region)
        self.inspect.Bind(wx.EVT_BUTTON, lambda event: self.start("inspect"))
        self.repair.Bind(wx.EVT_BUTTON, lambda event: self.start("repair"))
        self.cancel.Bind(wx.EVT_BUTTON, self.request_cancel)
        self.report.Bind(wx.EVT_BUTTON, self.show_report)
        self.open.Bind(wx.EVT_BUTTON, self.open_candidate)
        self.save_as.Bind(wx.EVT_BUTTON, lambda event: self.save_result(False))
        self.adopt.Bind(wx.EVT_BUTTON, lambda event: self.save_result(True))
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.tick, self.timer)
        self.timer.Start(250)
        self.show_region_fields()
        self.enable_controls()

    def clear_refill(self):
        self.refill_zones=[]
        self.refill_target_ids=[]
        self.refill_button.SetLabel('局部重填铜：默认不授权（选择铺铜对象）')

    def select_from_editor(self, _event):
        if self.busy() or self.control_pending or not self.inspected:
            return
        try:
            import pcbnew
            from .native_selection import saved_target_selection
            result = saved_target_selection(pcbnew, self.inspected, self.board.GetPath(),
                self.layer.GetStringSelection(), lambda: bridge.source_still_matches(self.config, self.inspected))
            wanted = set(result['target_ids'])
            self.target.SetCheckedItems([i for i, row in enumerate(self.targets) if row['id'] in wanted])
            self.mode.SetSelection(0)
            self.reviewed_recommendations = []
            self.clear_refill()
            self.describe_region()
            self.status.SetValue(result['notice'])
        except Exception as exc:
            self.fail(str(exc))

    def configure_refill(self, _event):
        if self.busy() or not self.inspected:
            return
        try:
            if self.mode.GetSelection()!=0:
                raise ValueError('请先勾选检查发现的连接；按网络名模式不隐式授权铺铜')
            targets=[self.targets[i] for i in self.target.GetCheckedItems()]
            layer=self.layer.GetStringSelection()
            choices=bridge.refill_choices(self.inspected,layer,targets,
                                         (self.advanced_options or {}).get('route_layers'))
            if not choices:
                raise ValueError('没有同时适用于所选连接的未锁定异网铺铜；不会扩大范围或自动选择其他对象')
            labels=[z['net']+' / '+','.join(z['layers'])+' / 范围 '+str(z['bounds_mm'])+' / '+z['uuid'] for z in choices]
            with wx.MultiChoiceDialog(self,
                    '仅允许勾选铺铜在已授权铜层、每个连接的固定局部范围内重填。\n铺铜定义、区域外铜和原连接仍受保护；跨层及新孔上限由修复权限决定。\n跨层重填暂需自动组合，并保持原走线/过孔固定。\n取消全部勾选并确认可撤回授权。',
                    '选择局部重填铜对象（默认不勾选）',labels) as dialog:
                dialog.SetSelections([i for i,z in enumerate(choices) if z['uuid'] in self.refill_zones])
                if dialog.ShowModal()!=wx.ID_OK:return
                selected=[choices[i]['uuid'] for i in dialog.GetSelections()]
            options=self.advanced_options
            # Explicit selection enables auto proposals without granting old
            # track motion, extra layers or vias. Existing permissions remain
            # intact; incompatible selections must be corrected by the user.
            if selected and options is None and not self.reviewed_recommendations:
                options=dict(route_layers=[layer],max_new_vias=0,reroute_nets=[],max_modified_tracks=0,strategy='auto')
            bridge.refill_arguments(selected,self.inspected,layer,'batch',[t['id'] for t in targets],
                                    options,self.reviewed_recommendations,self.recommendation_mode)
            self.refill_zones=selected
            self.refill_target_ids=[t['id'] for t in targets]
            if options is not self.advanced_options:
                self.advanced_options=options
                self.advanced.SetLabel('自动组合：所选单层，不改旧线、不新增孔')
            self.refill_button.SetLabel('局部重填铜：已选择 %d 个铺铜；对 %d 个连接分别限制范围'%(len(selected),len(targets)) if selected else '局部重填铜：默认不授权（选择铺铜对象）')
            self.status.SetValue('已选择 %d 个铺铜对象。只有启动修复才生效；无法完整验证的候选不会接受。'%len(selected))
        except Exception as exc:
            self.fail(str(exc))

    def review_recommendations(self, _event):
        if self.busy() or not self.inspected:
            return
        selected={self.targets[i]['id'] for i in self.target.GetCheckedItems()}
        proposals=[p for p in self.inspected.get('modification_recommendations',[]) if p['target_id'] in selected]
        if not proposals:
            self.status.SetValue('所选连接没有可见证的局部修改建议；不会自动扩大范围。')
            return
        labels=[]
        for p in proposals:
            detail='; '.join(o['uuid']+' '+o['net']+' '+str(o['start_mm'])+'→'+str(o['end_mm']) for o in p['objects'])
            scope='仅区域内片段；两端及区域外保持原样' if p.get('scope')=='local_middle' else '整段均在区域内'
            labels.append('目标 '+p['target_id']+' / '+p['layer']+' / '+scope+' / ROI '+str(p['region_mm'])+' / 修改额度 '+str(p['modification']['max_modified_tracks'])+'段：'+detail)
        with wx.MultiChoiceDialog(self,'选择明确允许修改的原线段集合；每个连接最多选一组。取消保留原授权。','确认局部修改对象',labels) as dialog:
            if dialog.ShowModal()!=wx.ID_OK:return
            chosen=[proposals[i] for i in dialog.GetSelections()]
        if len({p['target_id'] for p in chosen})!=len(chosen):
            self.fail('每个连接最多选择一组建议');return
        labels=['仅推挤'] if any(p.get('scope')=='local_middle' for p in chosen) else ['仅推挤','仅拆线重布','允许推挤或拆线重布']
        mode='shove'
        if chosen:
            with wx.SingleChoiceDialog(self,'仅作用于刚才列出的对象，不增加过孔或扩大区域。','确认允许的操作',labels) as operations:
                if operations.ShowModal()!=wx.ID_OK:return
                mode=('shove','ripup','shove+ripup')[operations.GetSelection()]
        self.recommendation_mode=mode
        self.reviewed_recommendations=[p['id'] for p in chosen]
        self.advanced_options=None
        self.advanced.SetLabel('已确认局部对象建议：'+mode+'，自动组合；不新增孔')
        self.status.SetValue('已确认 '+str(len(chosen))+' 组对象。授权只在您启动修复后生效。取消全部勾选并确认可撤回。')

    def show_comparison(self, _event):
        if not self.task or self.busy():return
        try:bridge.open_comparison(self.task)
        except Exception as exc:self.fail(str(exc))

    def configure_advanced(self, _event):
        if not self.inspected or self.busy():
            return
        layers, nets = bridge.advanced_choices(self.inspected)
        layer = self.layer.GetStringSelection()
        current = self.advanced_options or dict(route_layers=[layer], max_new_vias=0,
                                               reroute_nets=[], max_modified_tracks=0, strategy="krt")
        dialog = wx.Dialog(self, title="高级修复权限（每条连接独立限额）", size=(650, 650))
        layout = wx.BoxSizer(wx.VERTICAL)
        text = wx.StaticText(dialog, label="只授权明确勾选的操作；锁定对象和区域外对象始终保护。\n"
                             "普通通孔需要勾选全部铜层。移动/拆线仅允许所选普通网络的合格局部直线段，额度不足会停止。\n"
                             "推挤单次只在所选层进行，原孔与铺铜定义保持不变。\n"
                             "自动策略可尝试跨层固定铜补线并复用同网贯通孔；不能保证与拆线或重填铜组合。")
        text.Wrap(600); layout.Add(text, 0, wx.ALL, 12)
        strategy = wx.Choice(dialog, choices=["补线 / 受限拆线重布（可跨层）", "推挤已有走线（当前为单层、无新孔）", "自动组合（勾选网络即授权推挤和受限重布）"])
        strategy.SetSelection(("krt", "pns", "auto").index(current["strategy"]))
        layout.Add(strategy, 0, wx.ALL | wx.EXPAND, 8)
        layer_list = wx.CheckListBox(dialog, choices=layers, size=(-1, 100))
        layer_list.SetCheckedItems([i for i, name in enumerate(layers) if name in current["route_layers"]])
        layout.Add(layer_list, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        layout.Add(wx.StaticText(dialog, label="最多新增普通通孔（0 = 不新增孔；已授权层仍可尝试复用原有同网孔）"), 0, wx.ALL, 8)
        via_limit = wx.SpinCtrl(dialog, min=0, max=32, initial=current["max_new_vias"])
        layout.Add(via_limit, 0, wx.LEFT, 12)
        layout.Add(wx.StaticText(dialog, label="允许移动 / 重布的普通信号网络（不选 = 保持所有旧线）"), 0, wx.ALL, 8)
        net_list = wx.CheckListBox(dialog, choices=nets, size=(-1, 150))
        net_list.SetCheckedItems([i for i, name in enumerate(nets) if name in current["reroute_nets"]])
        layout.Add(net_list, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, 12)
        layout.Add(wx.StaticText(dialog, label="最多修改原有直线段（选择重布网络时设为 1–32）"), 0, wx.ALL, 8)
        track_limit = wx.SpinCtrl(dialog, min=0, max=32, initial=current["max_modified_tracks"])
        layout.Add(track_limit, 0, wx.LEFT, 12)
        layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL | wx.ALIGN_RIGHT, 12)
        dialog.SetSizer(layout)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            options = dict(route_layers=[layers[i] for i in layer_list.GetCheckedItems()],
                           max_new_vias=via_limit.GetValue(),
                           reroute_nets=[nets[i] for i in net_list.GetCheckedItems()],
                           max_modified_tracks=track_limit.GetValue(),
                           strategy=("krt", "pns", "auto")[strategy.GetSelection()])
            bridge.advanced_arguments(options, self.inspected, layer)
            self.reviewed_recommendations = []
            self.advanced_options = options
            self.advanced.SetLabel(({"pns":"推挤权限：", "krt":"补线/重布权限：", "auto":"自动组合权限："}[options["strategy"]]) + "%s；最多 %d 个过孔；%d 个网络 / %d 段" %
                (", ".join(options["route_layers"]), options["max_new_vias"],
                 len(options["reroute_nets"]), options["max_modified_tracks"]))
        except Exception as exc:
            self.fail(str(exc))
        finally:
            dialog.Destroy()

    def show_region_fields(self):
        manual = not self.auto_region.GetValue()
        self.roi_label.Show(manual)
        self.roi_row.ShowItems(manual)
        self.mode.Show(manual)
        self.net.Show(manual)
        self.GetChildren()[0].Layout()

    def scope_changed(self, _event):
        self.inspected = self.inspected_selection = self.inspected_path = None
        self.targets = []
        self.target.SetItems([])
        self.clear_refill()
        if self.auto_region.GetValue():
            self.mode.SetSelection(0)
        self.show_region_fields()
        self.describe_region()
        self.enable_controls()

    def describe_region(self, _event=None):
        checked = list(self.target.GetCheckedItems())
        selected_ids=[self.targets[i]['id'] for i in checked if i<len(self.targets)]
        if self.refill_zones and selected_ids!=self.refill_target_ids:
            self.clear_refill()
        index = checked[0] if checked else -1
        if len(checked) > 1:
            text = "已勾选 %d 条，将按所选顺序策略逐个修复。每条保持各自的局部范围，仅累积通过验收的修改。" % len(checked)
        elif self.auto_region.GetValue() and 0 <= index < len(self.targets):
            box = self.targets[index]["region_mm"]
            text = "自动范围：%.2f × %.2f mm，包含所选两端周围各 2 mm；所有新增或授权修改均须留在此范围内。" % (box[2] - box[0], box[3] - box[1])
        elif self.auto_region.GetValue():
            text = "选择连接后自动确定范围；两端周围各 2 mm，单边不超过 25 mm。失败不会自动扩大。"
        else:
            text = "高级手工范围：所有新增或授权修改均须留在填写的矩形内。"
        self.region_summary.SetLabel(text)
        self.region_summary.Wrap(790)
        self.GetChildren()[0].Layout()

    def busy(self):
        return self.starting or (self.task is not None and not self.finished)

    def enable_controls(self):
        idle = not self.busy() and not self.control_pending
        for widget in self.inputs:
            widget.Enable(idle)
        self.mode.Enable(idle and not self.auto_region.GetValue())
        self.net.Enable(idle and not self.auto_region.GetValue())
        self.inspect.Enable(idle)
        self.advanced.Enable(idle and self.inspected is not None)
        self.refill_button.Enable(idle and self.inspected is not None)
        self.from_selection.Enable(idle and self.inspected is not None)
        self.repair.Enable(idle and self.inspected is not None and bool(self.targets or self.mode.GetSelection() == 1))
        self.cancel.Enable(self.busy() and not self.cancel_requested.is_set())
        self.report.Enable(self.task is not None)
        self.open.Enable(idle and self.task is not None and self.finished
                         and self.task.get("accepted", False) and not self.cancel_requested.is_set())
        # A reopened plugin can load an existing result, allowing adoption
        # after the user has closed the original editor safely.
        self.save_as.Enable(idle)
        self.adopt.Enable(idle)

    def background(self, action, callback):
        def work():
            try:
                value, error = action(), None
            except Exception as exc:
                value, error = None, type(exc).__name__ + ": " + str(exc)
            self.events.put((callback, value, error))
        threading.Thread(target=work, daemon=True).start()

    def fail(self, message):
        self.status.SetValue(message)
        self.enable_controls()

    def start(self, kind):
        if self.busy() or self.control_pending:
            return
        try:
            selection = bridge.parameters(self.board.GetPath(), [field.GetValue() for field in self.roi],
                                          self.layer.GetStringSelection(), kind == "import-legacy" or self.auto_region.GetValue())
            target, inspected = None, None
            if kind == "repair":
                if selection != self.inspected_selection or self.inspected is None:
                    raise ValueError("磁盘板、ROI 或铜层与上次检查不同，请重新检查")
                if not self.ordinary.GetValue():
                    raise ValueError("请先确认普通单端信号的设计用途")
                if self.mode.GetSelection() == 0:
                    checked = list(self.target.GetCheckedItems())
                    if not checked or any(i >= len(self.targets) for i in checked):
                        raise ValueError("请勾选至少一个检查发现的待修复连接")
                    target = [self.targets[i]["id"] for i in checked]
                    kind = "batch"
                else:
                    name = self.net.GetValue()
                    if not name:
                        raise ValueError("请输入完整网络名；不做空白裁剪或通配匹配")
                    target = ("--net", name)
                inspected = self.inspected
            else:
                self.advanced_options = None
                self.reviewed_recommendations = []
                self.clear_refill()
                self.advanced.SetLabel("高级权限：默认仅补线，不改旧线、不打孔")
                self.inspected = self.inspected_selection = None
                self.inspected_path = None
                self.targets = []
                self.target.SetItems([])
                self.identity.SetValue("")
            self.starting = True
            self.task = None
            self.finished = False
            self.cancel_requested = threading.Event()
            self.cancel_acknowledged = False
            self.status.SetValue("准备独立任务；输入和规则将由 CLI 建立副本。")
            self.enable_controls()
            self.background(lambda: bridge.launch(self.config, kind, selection, target,
                                                   self.cancel_requested, inspected, self.inspected_path,
                                                   self.advanced_options if kind != "inspect" else None,
                                                   self.reviewed_recommendations if kind != "inspect" else None,
                                                   "joint" if self.joint_routes.GetValue() else "coordinated" if self.order_optimized.GetValue() else "selected",
                                                   self.recommendation_mode,
                                                   self.refill_zones if kind in ('repair','batch') else None), self.started)
        except Exception as exc:
            self.fail(str(exc))

    def started(self, task, error):
        self.starting = False
        if error or task is None:
            self.finished = True
            self.fail(error or "任务在启动前已取消。")
            self.maybe_close()
            return
        self.task = task
        self.status.SetValue("已启动 " + task["kind"] + "\n" + str(task["folder"]))
        self.enable_controls()

    def tick(self, _event):
        while True:
            try:
                callback, value, error = self.events.get_nowait()
            except queue.Empty:
                break
            callback(value, error)
        if not self.task or self.finished or self.control_pending:
            return
        task = self.task
        if time.monotonic() - self.last_poll < 0.75:
            return
        self.last_poll = time.monotonic()
        # CLI creates the task directory itself. A cancel before status.json
        # exists stays pending and is retried after startup; no mkdir race.
        if not (task["folder"] / "status.json").is_file():
            if task["process"].poll() is not None:
                self.finish({"status": "error", "reason": "CLI 未建立状态文件；请查看启动日志"})
            return
        command = "cancel" if self.cancel_requested.is_set() and not self.cancel_acknowledged else "status"
        self.control_pending = True
        self.background(lambda: bridge.query(self.config, command, task["folder"]),
                        lambda value, error: self.got_status(command, value, error))

    def got_status(self, command, state, error):
        self.control_pending = False
        if error:
            if self.task["process"].poll() is not None:
                self.finish({"status": "error", "reason": error})
            else:
                self.status.SetValue("状态/取消尚未确认，任务窗口保持打开。\n" + error)
            return
        if command == "cancel":
            self.cancel_acknowledged = bool(state.get("cancellation_requested") or state.get("status") in bridge.TERMINAL)
        elapsed = time.monotonic() - self.task["started"]
        self.status.SetValue("状态：{status}　阶段：{phase}　{elapsed:.1f} 秒\n{folder}\n{reason}".format(
            status=state.get("status", "未知"), phase=state.get("phase", ""), elapsed=elapsed,
            folder=self.task["folder"], reason=state.get("reason", "")))
        if state.get("status") in bridge.TERMINAL and self.task["process"].poll() is not None:
            self.finish(state)

    def finish(self, state):
        self.finished = True
        code = self.task["process"].poll()
        if code != 0 and state.get("status") in {"accepted", "complete"}:
            state = {"status": "error", "reason": "CLI 非零退出，残留成功状态不能被采用"}
        self.task["accepted"] = code == 0 and state.get("status") == "accepted" and state.get("accepted") is True
        path = self.task["folder"] / "result.json"
        try:
            result = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if result.get("status") != state.get("status"):
                self.task["accepted"] = False
            if self.task["kind"] == "inspect" and code == 0 and state.get("status") == "complete":
                if not result.get("source_files_untouched") or not result.get("source_hashes"):
                    raise ValueError("检查没有绑定完整输入身份")
                listing = json.loads((self.task["folder"] / "targets.json").read_text(encoding="utf-8"))
                self.inspected = result
                self.inspected_path = path
                self.inspected_selection = self.task["selection"]
                self.targets = [row for row in listing["targets"] if row.get("supported")]
                choices = []
                for row in self.targets:
                    pads = " → ".join(p["reference"] + ":" + p["number"] for p in row["pads"])
                    choices.append(row["net"] + "  " + pads + "  [" + row["id"] + "]")
                self.target.SetItems(choices)
                source = result["source_board"]
                self.identity.SetValue("已检查磁盘板：" + source + "\nSHA256: " + result["source_hashes"].get(source, "未知")
                                       + "\n待修复连接：" + str(len(self.targets)) + " 条；只列出未连通且符合范围的目标，最多 64 条。")
                self.describe_region()
            elif self.task['kind'] == 'import-legacy' and code == 0 and state.get('status') == 'complete':
                imported=bridge.imported_project(self.config,self.task)
                self.board.SetPath(imported)
                self.inspected = self.inspected_selection = self.inspected_path = None
                self.targets=[]; self.target.SetItems([])
                self.identity.SetValue('已选择独立导入工程：'+imported+'\n请重新检查目标；原磁盘板和编辑器未保存工作均未改动。')
            elif self.task["kind"] == "inspect":
                self.target.SetItems([])
            self.status.SetValue("结果：" + str(state.get("status")) + "；CLI 退出码：" + str(code) + "\n"
                                 + str(result.get("reason") or state.get("reason", "")) + "\n" + str(self.task["folder"]))
            if result.get('diagnosis'):
                diagnosis=result['diagnosis']
                self.status.AppendText("\n"+diagnosis['summary']+"\n"+diagnosis['suggestion'])
                for item in diagnosis.get('objects',[])[:5]:
                    self.status.AppendText("\n相关对象："+str(item.get('uuid'))+" / "+str(item.get('net'))+" / "+str(item.get('layer')))
            refill_text=bridge.refill_summary(result)
            if refill_text:self.status.AppendText('\n'+refill_text)
            if self.task["kind"] == "batch":
                self.status.AppendText("\n队列结果：" + str(result.get("batch_outcome", "stopped")) + "\n逐项统计：" + json.dumps(result.get("counts", {}), ensure_ascii=False))
                self.inspected = self.inspected_selection = self.inspected_path = None
                self.targets = []
                self.target.SetItems([])
                self.clear_refill()
            if self.cancel_requested.is_set():
                self.status.AppendText("\n已请求取消；候选打开保持禁用。监督进程已退出。")
        except Exception as exc:
            self.task["accepted"] = False
            self.status.SetValue("结果读取失败，不能采用候选：" + str(exc))
        self.enable_controls()
        self.maybe_close()

    def request_cancel(self, _event=None):
        self.cancel_requested.set()
        self.status.AppendText("\n取消请求待确认；启动尚未完成时会在状态文件出现后重试。")
        self.enable_controls()

    def show_report(self, _event):
        if self.task:
            path = self.task["folder"] / "report.md"
            if not path.is_file():
                path = self.task["log"]
            # Nonblocking launch of the default text viewer, never a PCB API.
            subprocess.Popen(["/usr/bin/open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_candidate(self, _event):
        if self.busy() or self.control_pending or not self.task.get("accepted"):
            return
        self.control_pending = True
        self.enable_controls()
        self.status.AppendText("\n打开前重新检查源和候选哈希…")
        self.background(lambda: bridge.open_candidate(self.config, self.task), self.opened)

    def opened(self, _state, error):
        self.control_pending = False
        if error:
            self.task["accepted"] = False
            self.status.AppendText("\n未打开候选：" + error)
        else:
            self.status.AppendText("\n已请求打开独立候选窗口；原编辑器不受本插件操作。")
        self.enable_controls()
        self.maybe_close()

    def save_result(self, adopt):
        if self.busy() or self.control_pending:
            return
        task_dir = self.task["folder"] if self.task and self.task.get("accepted") and not self.cancel_requested.is_set() else None
        if task_dir is None:
            with wx.FileDialog(self, "选择已验收任务的 result.json", wildcard="任务结果 (result.json)|result.json",
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                task_dir = Path(dialog.GetPath()).parent
        destination = None
        try:
            if adopt:
                report = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
                message = ("将备份并替换以下磁盘原板：\n" + report["source_board"] +
                           "\n\n请先处理未保存修改并关闭原工程，采纳过程中保持关闭。存在 KiCad 锁文件会拒绝。"
                           "\n只采纳已验收的修复；批量任务可能部分完成，请先查看逐项报告。"
                           "\n不会自动保存或关闭编辑器，原理图和规则不覆盖。确认采纳？")
                if wx.MessageBox(message, "确认采纳到原工程", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) != wx.YES:
                    return
            else:
                with wx.DirDialog(self, "选择保存位置（将在其中新建修复结果目录）", style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST) as dialog:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    destination = Path(dialog.GetPath()) / ("PCB修复结果-" + time.strftime("%Y%m%d-%H%M%S") + "-" + bridge.uuid.uuid4().hex[:6])
            self.control_pending = True
            self.enable_controls()
            self.status.AppendText("\n正在复核验收结果并" + ("备份、采纳原板…" if adopt else "另存工程副本…"))
            self.background(lambda: bridge.deliver(self.config, task_dir, destination=destination, adopt=adopt), self.saved_result)
        except Exception as exc:
            self.fail(str(exc))

    def saved_result(self, result, error):
        self.control_pending = False
        if error:
            self.status.AppendText("\n保存操作未确认完成：" + error + "\n若提示原工程被占用，请关闭后重开插件，再通过保存按钮选择本任务 result.json。")
        else:
            self.status.AppendText("\n结果：" + result["status"] + "\n板文件：" + result["board"])
            if result.get("backup_board"):
                self.status.AppendText("\n原板备份：" + result["backup_board"] + "\n原板已改变，旧任务不再作为‘原板未变’的验收凭据；请重新打开原工程。")
                if self.task:
                    self.task["accepted"] = False
                self.inspected = self.inspected_selection = self.inspected_path = None
            if result.get("warning"):
                self.status.AppendText("\n需核对：" + result["warning"])
        self.enable_controls()
        self.maybe_close()

    def on_close(self, event):
        if self.busy() or self.control_pending:
            self.close_requested = True
            if self.busy():
                self.request_cancel()
            if event.CanVeto():
                event.Veto()
            self.status.AppendText("\n等待任务/状态操作结束后关闭窗口。取消未确认时不销毁监督界面。")
            return
        self.timer.Stop()
        self.Destroy()

    def maybe_close(self):
        if self.close_requested and not self.busy() and not self.control_pending:
            self.Close()
