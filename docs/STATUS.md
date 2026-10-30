# 验证记录 / Validation record

版本 **0.9.5.dev13**，最小源码目录整理日期 **2026-10-30**。

## 本次目录检查 / Checks on this source distribution

| 项目 / Check | 结果 / Result |
| --- | --- |
| 运行代码与插件身份 / Runtime and plugin identity | 77 个 Python 文件与 dev13 归档逐字节一致 / 77 Python files identical to the dev13 archive |
| 代码回归 / Code regressions | 834 项：首次 831 通过、3 项因未提供固定外部源码跳过；配置源码后补充 3 项全部通过 / 831 passed initially, 3 explicitly skipped, then all 3 passed with pinned source |
| 插件代码检查 / Plugin checks | 23 项通过 / 23 passed |
| 独立 CLI 安装 / Separate CLI installation | 从此目录构建 wheel，安装到新虚拟环境，版本及帮助正常 / Wheel built here, installed in a new venv, version/help passed |
| 补丁清单 / Patch catalog | 所有收录补丁的固定哈希核对通过 / Included patch identities validated |
| 原生保护正例 / Native positive control | 自建局部中段候选 accepted，21 项适用检查通过 / Authored local-middle candidate accepted, 21 applicable checks passed |
| 原生保护反例 / Native negative control | 区域外尾段偏移 1 nm，明确拒绝为 protection_violation / 1 nm outside-tail change rejected |

测试从独立的桌面目录运行，代码不依赖历史研究脚本。代码回归复用本机已固定的 Python 几何环境；3 项外部源码检查复用固定补丁 checkout；原生正反例复用已配置的 KiCad/引擎运行时。本次没有重新下载或重建这些外部依赖，也没有进行 GUI 点击操作。

Tests ran from the separate source directory without historical research scripts. Code tests reused the pinned Python geometry environment; the three source integration checks reused the pinned patched checkout; native checks reused configured runtimes. External engines were not downloaded or rebuilt for this export. No GUI click-through test was performed.

21 项是这个示例的适用检查数，不是所有任务的固定数量。正例路径为人工构造，用于测试验收；反例用于确认区外保护。两者均不计为新增真实工程自动修复。

The 21 checks apply to this fixture, not every task. The positive route is manually constructed for acceptance testing, and the negative fixture exercises outside-region protection. Neither is counted as a new real-board automatic repair.

机器可读摘要见 [validation-summary.json](validation-summary.json)。代码测试复现见 [tests/README.md](../tests/README.md)；原生例复现见 [示例说明](../examples/local-middle-native/README.md)。

See [validation-summary.json](validation-summary.json), [code test instructions](../tests/README.md), and the [native example](../examples/local-middle-native/README.md).

## 最小目录范围 / Minimal repository scope

包含运行源码、插件、必需获取/构建脚本、补丁及固定版本、回归测试和一个自建保护示例。不包含第三方工程板、用户数据、虚拟环境、预编译引擎、运行日志、历史研究输出或安装路径配置。

Runtime code, the launcher, required setup/build recipes, pins, patches, regression tests and one authored protection example are included. Third-party boards, user data, virtual environments, compiled engines, run logs, historical research outputs and machine configurations are excluded.

源码版功能不变。仅剔除历史大型证据包生成器及其 17 项专属打包测试；source-lock/runtime-lock 保留运行所需固定身份，省略无关研究库存和本机绝对路径。没有将实验分支升级为这个版本的功能。

Runtime functionality is unchanged. Only the historical full evidence-archive builder and its 17 packaging-specific tests are omitted. Source/runtime locks retain required pins while removing unrelated inventory and machine paths. Experimental branches have not been promoted into this version.
