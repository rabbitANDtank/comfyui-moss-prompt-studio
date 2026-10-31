"""caption 分段通用拆装：姿势/服装分段标记与已知遮挡后缀的切分、拼回与替换。"""
from __future__ import annotations

# 姿势/服装注入：拆解时以图库描述替代角色动作/服装措辞。与前端
# web/js/editor/pose.js 的同名常量保持一致（caption 末尾的分段标记，供画布
# "对调两人姿势/服装"交换用）。两段可以共存，顺序固定为 姿势 → 服装。
POSE_MARKER = "；姿势："


COSTUME_MARKER = "；服装："


# 画布"重叠检测"追加的已知遮挡后缀，与 web/js/editor/model.js 的
# OCCL_FRONT / OCCL_BACK 逐字节一致。只有这两个精确串会被当作分段之后的
# 尾随文本保留；其他内容（含手填描述里的逗号分段）都属于最后一个分段，替换时整段丢弃。
OCCL_FRONT = "，处于最前层，部分遮挡相邻元素"
OCCL_BACK = "，被前方元素部分遮挡"
_OCCL_SUFFIXES = (OCCL_FRONT, OCCL_BACK)
# caption 分段顺序：姿势在前、服装在后。新增第三种注入时在这里追加即可。
_CAPTION_SEGMENTS = (("pose", POSE_MARKER), ("costume", COSTUME_MARKER))


def _split_caption_segments(caption: str):
    """把角色 caption 切成 (head, {segment_key: text}, tail)。

    head 是第一个分段标记之前的角色描述；tail 只保留已知遮挡后缀。未识别的
    残片并回 head（不丢弃用户手写内容）。同一 key 出现多次时后者覆盖前者，
    与旧版 `_pose_set_caption` 的 rindex 语义一致。
    """
    cap = str(caption or "")
    hits = []
    for key, marker in _CAPTION_SEGMENTS:
        pos = cap.find(marker)
        if pos >= 0:
            hits.append((pos, key, marker))
    if not hits:
        return cap, {}, ""
    pos, _, _ = min(hits, key=lambda h: h[0])
    head = cap[:pos].rstrip()
    body = cap[pos:]
    tail = ""
    for suf in _OCCL_SUFFIXES:
        if suf and body.endswith(suf):
            tail = suf
            body = body[:-len(suf)]
            break
    segments = {}
    i = 0
    size = len(body)
    while i < size:
        hit = next(((key, marker) for key, marker in _CAPTION_SEGMENTS
                    if body.startswith(marker, i)), None)
        if hit is None:
            nxt = min((p for p in (body.find(m, i) for _, m in _CAPTION_SEGMENTS) if p >= 0),
                      default=size)
            head = (head + body[i:nxt]).rstrip()
            i = nxt
            continue
        key, marker = hit
        start = i + len(marker)
        nxt = min((p for p in (body.find(m, start) for _, m in _CAPTION_SEGMENTS) if p >= 0),
                  default=size)
        segments[key] = body[start:nxt].strip()
        i = nxt
    return head, segments, tail


def _join_caption_segments(head: str, segments: dict, tail: str = "") -> str:
    """按固定顺序（姿势 → 服装）拼回 caption；空分段不写标记。"""
    out = (head or "").rstrip()
    for key, marker in _CAPTION_SEGMENTS:
        text = str(segments.get(key) or "").strip()
        if text:
            out += marker + text
    return out + (tail or "")


def _set_caption_segment(item: dict, key: str, text: str) -> None:
    """替换/追加一个分段，保留另一分段与已知遮挡后缀（幂等）。"""
    head, segments, tail = _split_caption_segments(item.get("caption") or "")
    segments[key] = (text or "").strip()
    item["caption"] = _join_caption_segments(head, segments, tail)
