"""离线测试（不依赖 ComfyUI 运行环境）。

运行:  python tests/run_tests.py
自动收集 tests/py/test_*.py 中的 test_* 函数（按文件名排序 + 函数定义
顺序）逐个执行。不再维护手写的 TESTS 显式列表——新增测试函数放对文件
即可被发现，杜绝“新增用例忘注册漏跑”。
注意:  请勿同时运行多个测试进程——mock 走 loopback 短连接，并发时
Windows 会报 WinError 10053 使网络类用例偶发失败（v1.8 前即如此）。

覆盖: 官方口径编译(含官方文档给出的期望坐标) / 三形态识别与反解往返 /
      节点行为 / 翻译结构完整性校验 / endpoint 与请求体组装 /
      本地 mock 服务下的翻译 HTTP 成功-重试-失败路径。
"""
import glob
import importlib.util
import os
import sys
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, "py"))  # 测试模块 `from helpers import ...` 用


def _load(path):
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _collect():
    """glob tests/py/test_*.py，按文件名排序；模块内按 vars() 插入顺序（= 定义顺序）取 test_*。"""
    cases = []
    for path in sorted(glob.glob(os.path.join(HERE, "py", "test_*.py"))):
        mod = _load(path)
        for name, fn in vars(mod).items():
            if name.startswith("test_") and callable(fn):
                cases.append(fn)
    return cases


def main():
    failed = 0
    for fn in _collect():
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception:
            failed += 1
            print(f"[FAIL] {fn.__name__}")
            traceback.print_exc()
    print("=" * 46)
    print("ALL TESTS PASSED" if not failed else f"{failed} TEST(S) FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
