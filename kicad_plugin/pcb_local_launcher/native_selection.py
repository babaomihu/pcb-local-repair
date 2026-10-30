# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only hints for selecting already inspected, saved-board tasks.

This does NOT attest that the whole editor board is saved, export its memory,
or apply a repair to it. Only supported selected endpoint identities/positions
are compared with the disk inspection; all repair inputs remain disk files.
Call on the KiCad UI thread. Never use this result as an acceptance witness.
"""
from pathlib import Path
import math


def _point(point):
    return [int(point.x), int(point.y)]


def _nm(point):
    if len(point) != 2 or not all(math.isfinite(v) for v in point):
        raise ValueError('检查中的端点坐标无效，请重新检查磁盘板')
    return [round(v * 1000000) for v in point]


def saved_target_selection(api, inspected, saved_board, layer, check_disk):
    """Return matching existing target IDs, never new routing permissions.

``check_disk`` must re-enumerate and hash the saved board/project context using
the installed CLI's source_still_matches helper. It is mandatory and runs both
before and after reading editor selection. The caller must still use the normal
launch-time stale-inspection and original-work protection checks.
"""
    if str(api.Version()) != '10.0.4':
        raise ValueError('当前选择入口仅验证过 KiCad 10.0.4')
    if not callable(check_disk):
        raise ValueError('缺少磁盘工程检查，不能导入当前选择')
    saved_board = Path(saved_board).expanduser().resolve()
    if (inspected.get('status') != 'complete' or not inspected.get('source_files_untouched')
            or inspected.get('source_board') != str(saved_board)
            or inspected.get('discovery', {}).get('allowed_layer') != layer):
        raise ValueError('请先完整检查当前所选磁盘板和铜层')
    check_disk()
    active = api.GetBoard()
    if active is None or not active.GetFileName() or Path(active.GetFileName()).expanduser().resolve() != saved_board:
        raise ValueError('当前编辑板与已检查磁盘板不同；不会从其他板导入选择')
    native = inspected.get('preflight', {}).get('measurement', {}).get('snapshot', {})
    enabled = list(active.GetEnabledLayers().CuStack())
    layers = [str(active.GetStandardLayerName(value)) for value in enabled]
    if layers != native.get('copper_layers'):
        raise ValueError('当前编辑板层栈与磁盘检查不同，请保存后重新检查')
    selection = list(api.GetCurrentSelection())
    if not selection:
        raise ValueError('请先在 PCB Editor 中选中焊盘、直线段或普通通孔')
    if len(selection) > 128:
        raise ValueError('当前选择超过 128 个对象，请缩小选择范围')
    by_uuid = {}
    for row in native.get('items', {}).values():
        by_uuid.setdefault(row['uuid'], []).append(row)
    selected = []
    for item in selection:
        kind = {'PAD':'pad', 'PCB_TRACK':'segment', 'PCB_VIA':'via'}.get(str(item.GetClass()))
        if kind is None:
            raise ValueError('当前选择包含不支持的对象；请只选择焊盘、直线段或普通通孔')
        uid = str(item.m_Uuid.AsString())
        rows = by_uuid.get(uid, [])
        if len(rows) != 1 or rows[0].get('kind') != kind:
            raise ValueError('选中对象不在磁盘检查中或身份不唯一，请保存后重新检查')
        row = rows[0]
        copper = [str(active.GetStandardLayerName(value)) for value in item.GetLayerSet().CuStack() if value in enabled]
        parent_locked = kind == 'pad' and item.GetParentFootprint().IsLocked()
        if (str(item.GetNetname()) != row['net'] or copper != row['copper_layers'] or layer not in copper
                or _point(item.GetPosition()) != _nm(row['position_mm'])
                or bool(item.IsLocked() or parent_locked) != row['locked']):
            raise ValueError('选中端点的位置、网络、铜层或锁定状态已变化，请保存后重新检查')
        if kind == 'pad':
            if (str(item.GetNumber()) != row['number']
                    or str(item.GetParentFootprint().m_Uuid.AsString()) != row['footprint_uuid']):
                raise ValueError('选中焊盘的父对象或编号已变化，请保存后重新检查')
        elif kind == 'segment':
            if (_point(item.GetStart()) != _nm(row['start_mm']) or _point(item.GetEnd()) != _nm(row['end_mm'])
                    or int(item.GetWidth()) != round(row['width_mm'] * 1000000)):
                raise ValueError('选中线段的端点或宽度已变化，请保存后重新检查')
        elif row.get('standard_through') is not True or item.GetViaType() != api.VIATYPE_THROUGH:
            raise ValueError('当前选择仅支持已检查的普通通孔')
        selected.append(uid)
    check_disk()
    # Matching at least one selected endpoint narrows the existing task list.
    # A selection never synthesizes a task or changes its ROI/net/permissions.
    selected_set = set(selected)
    matches = []
    for target in inspected['discovery'].get('targets', []):
        endpoint_ids = {e['uuid'] for e in target.get('endpoints', [])} or set(target.get('pad_uuids', []))
        if target.get('supported') and selected_set.intersection(endpoint_ids):
            matches.append(target['id'])
    if not matches:
        raise ValueError('选中对象没有对应的已检查可修连接；不会改选整个网络')
    return dict(target_ids=matches, selected_uuids=sorted(selected_set),
                source_board=str(saved_board), layer=layer, endpoint_binding_only=True,
                active_board_saved_state_verified=False,
                notice='仅筛选已检查磁盘版本的连接。未保存编辑不参与修复；此选择不证明整个编辑板已保存。')
