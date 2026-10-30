# 安装与操作 / Installation and usage

本指南对应 **0.9.5.dev13**。命令从仓库根目录运行，示例使用 macOS / zsh；路径含空格时保持引号。已验证的是同一 macOS Apple Silicon 主机上的独立源码与环境安装，不是全新操作系统验证。

These instructions target **0.9.5.dev13** on macOS Apple Silicon. Run commands from the repository root. Independent environments were tested on the existing development host; a clean OS installation is not claimed. Native build recipes are currently specific to that platform.

## 1. 前置条件 / Prerequisites

- KiCad **10.0.4**，包含 PCB Editor 与自带 `pcbnew` Python。/ KiCad 10.0.4 with PCB Editor and its bundled `pcbnew` Python.
- Python **3.12**（记录版本 3.12.14）、Git、Xcode command line tools；KRT 构建脚本核对 Rust **1.98.0**。/ Python 3.12, Git, Apple compiler tools; the KRT recipe checks the pinned Rust 1.98.0 identity.
- 可选 PNS 与完整 DRC 原生构建需要 CMake、Ninja、wxWidgets 3.2、Boost 1.85 及 KiCad 开发依赖。当前脚本使用 `/opt/homebrew`，并检查本机工具。/ Optional PNS and complete-DRC native builds need system libraries and use `/opt/homebrew` paths.

已记录的开发库包括 wxWidgets 3.2.11、Boost 1.85.0、OpenCASCADE 7.9.3、libngspice 47，以及 protobuf、abseil、glew、glm、cairo、pixman、freetype、harfbuzz、fontconfig、libgit2、nng、zstd、unixODBC。编译脚本保存配置日志与实际工具链；缺失依赖时停止并保留日志。它们不会自动覆盖系统库。原生构建会占用数 GB 空间，源码仓库小不代表安装依赖也小。

The native recipes record the detected toolchain and preserve failed build logs. They do not install or replace system libraries automatically. The dependencies above are the recorded development environment, not a portable package-manager lock. Native builds may require several GB even though this source repository is small.

## 2. 安装 CLI / Install the CLI

```sh
python3.12 -m venv .venv-cli
.venv-cli/bin/python -m pip install -r requirements-cli-build.lock.txt
.venv-cli/bin/python -m pip install --no-deps --no-build-isolation .
.venv-cli/bin/pcb-local --version
```

预期版本 `0.9.5.dev13`。CLI 本身使用标准库；搜索所需的几何/数值库安装在独立 router 环境中。不要把 CLI 装入 KiCad 自带 Python。以下用 `.venv-cli/bin/pcb-local`；也可激活该虚拟环境后直接用 `pcb-local`。

Expected version: `0.9.5.dev13`. The CLI has no additional Python package dependencies; geometry/numerical dependencies live in the separate router environment. Do not install this CLI into KiCad's bundled Python.

## 3. 固定 KRT / Pinned KRT runtime

```sh
python3.12 scripts/fetch_krt.py --root "$PWD/runtime" --patch-set v0.8.9
python3.12 scripts/setup_krt.py --root "$PWD/runtime" --python python3.12 \
  --allow-recorded-patch --patch-set v0.8.9
python3.12 scripts/setup_krt.py --root "$PWD/runtime" --python python3.12 \
  --allow-recorded-patch --patch-set v0.8.9 --execute
```

首次获取要求 `runtime/KiCadRoutingTools` 不存在。`setup_krt.py` 默认只检查，`--execute` 才构建；目标、依赖版本、补丁字节或工具链不符会明确拒绝。所有历史 KRT 补丁也保留，因为当前目录清单会核对它们。固定 Python 包版本不是逐 wheel 文件哈希锁；不保证跨机器二进制逐字节一致。

The fetch command requires a new checkout destination. Setup is read-only without `--execute`. Source, patch and dependency identities are checked. Historical patch files are retained because the catalog validates them too. Package versions are pinned, but downloadable wheel archives are not individually hash-locked; cross-machine binary reproducibility is not claimed.

## 4. 完整 DRC 原生组件 / Complete native DRC collection

普通 KiCad 可以用于其检查覆盖足够的任务；报告触顶或覆盖证据不足会被拒绝。复杂工程推荐准备已验证的完整采集组件，不能通过关闭检查绕过失败。

Stock KiCad can be used where its report and coverage evidence are complete. Truncated reports or insufficient coverage fail closed. Prepare the following component for complex boards; do not bypass failures by disabling checks.

```sh
python3.12 scripts/setup_drc_complete.py \
  --source-root "$PWD/runtime/kicad-drc-source" \
  --build-dir "$PWD/runtime/kicad-drc-build" \
  --run-dir "$PWD/runtime/drc-build-log"
```

此脚本使用固定 KiCad 10.0.4 源码和 `patches/kicad10-drc-report-capacity.patch`，提升报告容量并输出覆盖见证；不放松设计规则或替换检查算法。SWIG 4.2.1 在独立构建目录准备。输出为 `runtime/kicad-drc-build/kicad/KiCad.app/Contents/MacOS/kicad-cli`；相邻 manifest 及原生模块/链接库也必须保留，不能只复制这个可执行文件。

This builds pinned KiCad 10.0.4 with increased reporting capacity and coverage evidence, without relaxing design rules or changing DRC algorithms. SWIG 4.2.1 is prepared separately. Keep the whole build runtime and its manifest; copying the CLI executable alone is insufficient.

## 5. 可选推挤运行时 / Optional shove runtime

```sh
python3.12 scripts/fetch_pns.py --destination "$PWD/runtime/PCBWorld-Engine" \
  --run-dir "$PWD/runtime/pns-fetch-log"
python3.12 scripts/setup_pns.py --engine-root "$PWD/runtime/PCBWorld-Engine" \
  --build-dir "$PWD/runtime/pns-build" \
  --venv-dir "$PWD/runtime/.venv-pns" --run-dir "$PWD/runtime/pns-build-log"
```

PNS 上游构建脚本不支持 engine/build/venv 路径中的空白字符。本引擎内部使用固定 KiCad 9.0.8；源板读取与最终验收仍用 KiCad 10.0.4。它只处理临时模型，不能把旧版本保存的整板直接替换原板。未准备 PNS 时，不保证需要推挤的任务可用。

PNS's upstream build paths must contain no whitespace. It uses pinned KiCad 9.0.8 internally on a temporary model; source-board interpretation and final acceptance remain on KiCad 10.0.4. Full boards saved by the older runtime are never treated as replacement originals. Tasks requiring shove need this optional runtime.

## 6. 配置 / Configure

完成上面构建后运行；未构建 PNS 时，省略两个 `--pns-*` 参数。配置输出必须是新文件。若使用系统 KiCad CLI，替换 `--kicad-cli` 路径即可，但报告完整性要求不变。

After building, run the following. Omit both PNS arguments if it is not installed. The output configuration must be a new file. You may point `--kicad-cli` at the system CLI, subject to the same completeness checks.

```sh
.venv-cli/bin/pcb-local configure \
  --krt-root "$PWD/runtime/KiCadRoutingTools" \
  --router-python "$PWD/runtime/.venv-krt/bin/python" \
  --kicad-cli "$PWD/runtime/kicad-drc-build/kicad/KiCad.app/Contents/MacOS/kicad-cli" \
  --kicad-python /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9 \
  --pns-python "$PWD/runtime/.venv-pns/bin/python" \
  --pns-module-dir "$PWD/runtime/pns-build/pcbnew/python/rl" \
  --output runtime.json
```

`configure` 固定当前工具身份，不代表修复能力已经验收。升级依赖后需重新配置并回归。`runtime.json` 含本机路径，不应提交 GitHub。

Configuration binds local runtime identities; it is not routing acceptance. Reconfigure and regress after dependency changes. Do not commit the machine-specific `runtime.json`.

## 使用 / Usage

基础检查、单项修复和另存见 [README](../README.md)。CLI 要求显式选层；插件可以传递层和任务。`--auto-region` 自动生成范围，但不会自动授予邻线移动、增加孔或改变铺铜的权限。

See [README](../README.md) for inspection, single-target repair, and export. The CLI needs an explicit layer; the plugin can pass the task/layer. Automatic regions do not automatically authorize track movement, new vias, or zone changes.

```sh
# 多选 / Selected queue: replace IDs with those returned by inspection.
.venv-cli/bin/pcb-local batch --config runtime.json --board /path/to/board.kicad_pcb \
  --auto-region --layer F.Cu --inspection tasks/inspect-001/result.json \
  --targets TARGET_ID_1 TARGET_ID_2 --signal-kind ordinary-single-ended \
  --candidate-strategy auto --order constrained-first --out-dir tasks/batch-001

# 状态、取消、解释 / Status, cancellation, explanation
.venv-cli/bin/pcb-local status tasks/batch-001
.venv-cli/bin/pcb-local cancel tasks/batch-001
.venv-cli/bin/pcb-local-explain --result tasks/batch-001/result.json --out-dir tasks/batch-001-explanation
```

高级权限参数按实际任务添加，不要直接照抄陌生工程的 UUID。/ Add explicit permissions as appropriate; never reuse UUIDs from unrelated boards.

| 参数 / Option | 用途 / Meaning |
| --- | --- |
| `--accept-recommendation ID --recommendation-mode shove` | 明确采纳本次检查绑定的修改建议 / Accept an exact recommendation bound to this inspection |
| `--local-track UUID` | 仅允许原直线在区域内中段移动，区域外尾段固定；repair/verify 使用，不与整网修改或建议混用 / In-region middle only; repair/verify, not combined with whole-net permissions or recommendations |
| `--modify-nets NAME --max-modified-tracks N` | auto 下允许该网的合格局部对象推挤/重布 / Explicit eligible local neighbor changes in auto mode |
| `--route-layers F.Cu B.Cu --max-new-vias 2` | 双层板有限通孔示例；多层板须完整列出跨越的铜层 / Bounded through vias on a two-layer board; multilayer boards require all traversed copper layers |
| `--refill-zone UUID` | 明确选择可在原局部区域重填的铺铜，可重复；区域外必须精确保持 / Select local refill zones; exact outside geometry remains protected |

读 `result.json`、`report.md`、`changes.json` 及适用的验证/对比文件。批量结果须逐项查看，不把部分完成当全部成功。`no_progress` 是当前方法、权限和预算内未找到合格解，不证明物理上不可布通。

Read `result.json`, `report.md`, `changes.json`, and applicable validation/comparison artifacts. Inspect each batch outcome; partial completion is not full success. `no_progress` means no accepted solution within this method, permission scope and budget, not physical impossibility.

### 保存 / Saving

```sh
# 首选另存 / Save an independent accepted result.
.venv-cli/bin/pcb-local export-result tasks/repair-001 --destination outputs/repaired-001

# 明确采纳 / Explicit adoption, only after closing the original editor.
.venv-cli/bin/pcb-local adopt-result tasks/repair-001 --original-closed
```

采纳会检查来源未变化、候选与验收证据一致且没有锁文件，备份后才替换原板。它不是合并未保存工作；先在 KiCad 保存或放弃未保存修改，再关闭原工程。失败或未验收候选不得采纳。

Adoption checks source identity, candidate/evidence integrity and editor locks, then backs up before replacing the PCB. It does not merge unsaved work. Save or discard unsaved edits in KiCad and close the original first. Failed/unverified candidates cannot be adopted.

### KiCad 插件 / KiCad launcher

```sh
.venv-cli/bin/python scripts/install_kicad_plugin.py \
  --cli-python "$PWD/.venv-cli/bin/python" --runtime-config "$PWD/runtime.json"
# 上一步诊断通过后 / After successful diagnostics:
.venv-cli/bin/python scripts/install_kicad_plugin.py \
  --cli-python "$PWD/.venv-cli/bin/python" --runtime-config "$PWD/runtime.json" --execute
```

安装后保存工作并重启 PCB Editor 或重载动作插件。插件可检查待修项、多选启动后台修复、取消、查看候选和报告、另存或明确采纳。已有插件目录会被保护而拒绝覆盖，先备份旧托管安装；不要同时注册两份。新版插件已做代码检查，未做新版 GUI 点击验收。

Save work and restart PCB Editor or reload action plugins. The launcher can inspect, select multiple targets, start/cancel background repairs, review results, export, or explicitly adopt. Existing plugin directories are not overwritten; back up an older managed installation first. The current launcher has code-level validation, not a new GUI click-through acceptance test.

## 故障排查 / Troubleshooting

- **环境或源码不匹配 / Identity mismatch**：检查固定提交、补丁、运行时路径；不要改验收代码绕过 / Verify pins, patches and paths rather than bypassing gates.
- **DRC 不完整 / Incomplete DRC**：查看 provider/容量/超时诊断；必要时构建完整采集组件 / Inspect coverage/capacity/timeouts; use the complete collector when needed.
- **几何不支持 / Unsupported geometry**：保留报告并缩小功能承诺，不把 unsupported 当修复成功 / Keep the report; unsupported is not success.
- **保护违规 / Protection violation**：即使目标已连接也拒绝；检查区外铜、锁定对象、规则或原连接变化 / Rejected even when connected; review protected changes.
- **目录已存在 / Existing output**：换一个新任务目录，旧日志和结果不覆盖 / Choose a fresh task directory.
- **进程中断 / Interrupted run**：先查状态和真实进程，不因观察超时重复启动 / Check status and actual processes before restarting.
