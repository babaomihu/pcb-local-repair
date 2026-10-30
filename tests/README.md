# 代码测试 / Code tests

从仓库根目录执行。/ Run from the repository root.

```sh
python3.12 -m venv .venv-tests
.venv-tests/bin/python -m pip install -r requirements-krt.lock.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests \
  .venv-tests/bin/python -m unittest discover -s tests -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 .venv-tests/bin/python kicad_plugin/selfcheck.py
```

多数测试不需要原生 KiCad 或已编译路由引擎，使用独立临时目录中的机制输入。几何测试需要固定 Shapely 等依赖。`test_krt_policy` 的三项集成检查，在未提供固定补丁源码时明确跳过；准备后可单独执行：

Most tests use synthetic inputs and temporary directories without native KiCad or a compiled router. Geometry tests need the pinned Python dependencies. Three `test_krt_policy` integration checks explicitly skip without the pinned patched source. Run them after preparing that source:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests \
  PCB_LOCAL_TEST_KRT_ROOT="$PWD/runtime/KiCadRoutingTools" \
  .venv-tests/bin/python -m unittest test_krt_policy -v
```

代码测试与插件检查不操作真实 GUI。原生几何保护正反例见 [examples/local-middle-native](../examples/local-middle-native/README.md)，需要配置好原生运行时。该例的合格路径为人工构造，用来验证验收保护，不计为自动布线成功。

Code and plugin checks do not exercise the actual GUI. See the [native protection example](../examples/local-middle-native/README.md) after configuring native runtimes. Its valid path is manually constructed to exercise acceptance, not an automatic routing success.

这个最小目录保留 dev13 的运行/保护回归测试，省略只服务历史大型证据包生成器的 `test_release_builder.py`（17 项）及依赖历史运行目录的手工实验脚本。功能代码没有因此删减。

This minimal repository retains dev13 runtime/protection regressions, omitting the 17 tests for the historical full evidence-archive builder and manual experiments tied to old run directories. Runtime functionality is unchanged.
