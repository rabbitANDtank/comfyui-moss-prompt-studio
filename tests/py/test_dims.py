"""snap16 尺寸对齐：.5 边界方向与夹取（审计 P2-20 测试缺口）。"""
from helpers import _expect  # noqa: F401  (helpers 先导入以初始化 sys.path)

from py.dims import snap16


def test_snap16_rounds_half_up():
    # 1000/16=62.5 曾因银行家舍入落到 992（方向随商奇偶漂移），必须恒为 1008
    assert snap16(1000) == 1008
    assert snap16(1008) == 1008
    assert snap16(1024) == 1024
    assert snap16(63) == 64 and snap16(64) == 64 and snap16(65) == 64
    assert snap16(1039) == 1040


def test_snap16_clamps():
    assert snap16(0) == 64
    assert snap16(-100) == 64
    assert snap16(99999) == 15360
    assert snap16(100, lo=128, hi=256) == 128
