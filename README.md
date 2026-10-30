# PCB Local Repair

**KiCad 局部布线修复工具 / Local PCB Routing Repair for KiCad**

[中文](#中文) · [English](#english) · [安装与操作 / Installation](docs/INSTALL.md)

## 中文

PCB Local Repair 用于修复 KiCad PCB 中选定的未连接项。选择需要修复的连接后，工具自动确定局部范围、尝试布线，并检查连接关系、设计规则和修改范围，生成可审查、可另存的修复结果。

提供 **KiCad 插件和命令行** 两种使用方式，支持单条连接修复与多条连接批量处理。布线任务在工程副本上运行，结果通过验收后，由用户决定另存或采纳。

### 主要功能

- **待修连接发现**：列出支持修复的未连接项，支持焊盘、已有走线与过孔端点。
- **自动确定范围**：根据选定连接生成局部修复区域，无需手动填写坐标。
- **多连接处理**：一次选择多条连接，按队列处理，并支持修复顺序调整和候选组合。
- **局部修复策略**：支持补线、受控推挤、拆线重布、有限跨层打孔及已有过孔复用。
- **精细修改权限**：指定允许移动的走线、允许重填的铺铜和可使用的层；支持只调整跨区走线的区域内部分。
- **原生检查与保护**：重新填铜后检查目标连接、原有连接和完整板级 DRC，核对规则与受保护区域。
- **结果审查与保存**：提供修改清单、前后对比、失败诊断和运行日志；合格结果可一键另存，也可备份后明确采纳到原工程。

### 使用流程

1. 按[安装文档](docs/INSTALL.md)准备环境并安装插件或 CLI。
2. 打开已保存的 PCB 工程，检查待修连接。
3. 选择一条或多条连接，设置允许的修改，启动修复。
4. 查看结果与修改清单，将通过验收的结果另存，或明确采纳。

命令行示例：

```sh
# 检查待修连接
pcb-local inspect --config runtime.json --board /path/to/board.kicad_pcb \
  --auto-region --layer F.Cu --out-dir tasks/inspect-001

# 将 TARGET_ID 替换为检查结果中的连接 ID
pcb-local repair --config runtime.json --board /path/to/board.kicad_pcb \
  --auto-region --layer F.Cu --inspection tasks/inspect-001/result.json \
  --target TARGET_ID --signal-kind ordinary-single-ended \
  --candidate-strategy auto --out-dir tasks/repair-001

# 另存通过验收的修复结果
pcb-local export-result tasks/repair-001 --destination outputs/repaired-001
```

当前版本 **0.9.5.dev13**，适用环境为 **macOS Apple Silicon、Python 3.12、KiCad 10.0.4**。面向普通单端信号的局部修复；具体参数、批量操作和插件安装见[使用文档](docs/INSTALL.md)。

## English

PCB Local Repair repairs selected unfinished connections in KiCad PCB projects. After you select a connection, it determines a local region, attempts routing, and checks connectivity, design rules, and modification permissions to produce a result you can review and save.

Use it through a **KiCad plugin or command-line interface**. It supports individual connections and selected batches. Routing runs on project copies; accepted results can be exported or explicitly adopted by the user.

### Features

- **Connection discovery:** list eligible unfinished connections involving pads, existing tracks, and vias.
- **Automatic local regions:** derive the repair area from the selected connection without manually entering coordinates.
- **Batch repair:** select multiple connections, process a queue, and use ordering and candidate-combination strategies.
- **Local routing strategies:** add tracks, perform controlled shove and rip-up/reroute, add a bounded number of through vias, and reuse existing vias.
- **Explicit permissions:** select movable tracks, refillable zones, and permitted layers; modify only the in-region portion of eligible tracks crossing the boundary.
- **Native validation:** refill zones, check target and existing connectivity, run full-board DRC, and verify rules and protected regions.
- **Review and delivery:** inspect change lists, before/after reports, failure diagnostics, and logs. Export accepted results or explicitly adopt them with a backup.

### Workflow

1. Follow the [installation guide](docs/INSTALL.md) to prepare the runtime and install the plugin or CLI.
2. Inspect a saved PCB project for eligible unfinished connections.
3. Select one or more connections, choose modification permissions, and start repair.
4. Review the changes, then export or explicitly adopt accepted results.

The CLI example above demonstrates inspection, repair, and export. Replace `TARGET_ID` with an ID returned by inspection.

Current version: **0.9.5.dev13**. Target environment: **macOS Apple Silicon, Python 3.12, KiCad 10.0.4**. The tool handles local repairs of ordinary single-ended signals. See the [usage guide](docs/INSTALL.md) for batch processing, permissions, and plugin setup.

## 目录 / Repository layout

```text
src/pcb_local/     CLI、修复策略与验收 / CLI, repair strategies and validation
kicad_plugin/      KiCad 插件 / KiCad launcher
scripts/          安装与构建脚本 / Setup and build scripts
patches/          依赖补丁 / Dependency patches
tests/            代码回归测试 / Regression tests
examples/         自建机制示例 / Authored mechanism examples
docs/             安装与测试文档 / Installation and validation documentation
licenses/         许可文件 / License texts
```

## 许可证 / License

项目代码采用 [GPL-3.0-or-later](LICENSE)。各文件的许可例外与归属见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

Project code is licensed under [GPL-3.0-or-later](LICENSE). File-specific exceptions and attribution are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
