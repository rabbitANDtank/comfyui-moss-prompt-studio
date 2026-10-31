"""画布尺寸工具：节点共享的 16 倍数对齐（避免各节点各写一份 _snap16）。"""


def snap16(v, lo=64, hi=15360):
    """把任意数值夹到 [lo, hi] 并对齐到最近的 16 倍数。"""
    v = int(v)
    return max(lo, min(hi, round(v / 16) * 16))
