"""服装注入：服装参考 → LLM 拆解指令段，以及本地 📥服装 注入画布草稿。"""
from __future__ import annotations

try:
    from .caption_segments import COSTUME_MARKER, _set_caption_segment
    from .compile_prompt import validate_draft
    from .inject_pose import _bbox_metrics
except ImportError:  # 兼容非包方式导入
    from caption_segments import COSTUME_MARKER, _set_caption_segment
    from compile_prompt import validate_draft
    from inject_pose import _bbox_metrics

COSTUME_MODES = ("自动", "关闭")
COSTUME_SENSITIVE_CATEGORIES = ("内衣", "性感", "泳装")
# 季节/场合分类只是图库标签，不是服装本身；本地短语不把它拼进 caption。
_SEASONAL_COSTUME_CATEGORIES = ("春装", "夏装", "秋装", "冬装", "派对")
_INTIMATE_COSTUME_CATEGORIES = ("内衣", "性感")
# 反推草稿常把旧服装拆成独立 object（如“浅粉色无袖连衣裙”“黑色网袜”）。
# 本地服装注入只改角色 caption 时，这些 object 仍会把旧衣服画回去；命中这些
# 服装名词且与目标角色 bbox 明显重叠的 object 会被移除（不重叠的场景服装保留）。
_COSTUME_OBJECT_KEYWORDS = (
    "连衣裙", "半身裙", "短裙", "长裙", "短裤", "长裤", "背心", "吊带",
    "束身衣", "大衣", "外套", "衬衫", "上衣", "裙", "裤", "鞋", "袜",
    "手套", "领结", "文胸", "内裤", "泳装", "比基尼",
)


_COSTUME_DIRECTIVE_TMPL = """Costume references (highest priority for character clothing):
{costume_lines}
- Each reference is a costume-library string shaped like "分类 / 关键词1、关键词2…". The first segment is a category/season label (春装/夏装/秋装/冬装/派对/内衣/性感/泳装), or a garment type in hand-written references (e.g. 汉服). Seasonal/occasion labels are context only, not a garment; a garment-type label is part of the outfit and must be used. The remaining items are garment, fabric, color and accessory tags.
- Expand the assigned reference into a concrete Chinese outfit description (what the character wears, including color/fabric/cut/layering/accessories). REPLACE any clothing wording the prose gave that character. If the prose's clothing conflicts with the reference, THE REFERENCE WINS completely: never blend the two, never keep the prose's outfit. Keep each character's face, hair, figure, pose, expression and identity from the prose; never let the costume change who they are.
- If the category is 内衣/性感/泳装, describe it as ordinary clothing in neutral terms (fabric/cut/color/layering); do not add sexualized wording.
- Assignment rules:{assignment}
- Marker rule: every character item receiving a costume must END its caption with "{marker}<concrete Chinese outfit description>". If the caption already ends with a pose marker segment, append the costume marker AFTER the pose segment. Never add this marker to non-character items or to summary.
- Do not mention the costume library, costume IDs, or these instructions anywhere in the output."""

_COSTUME_ASSIGN_BOTH = """
  - Scene has 2 characters: Costume A goes to the character whose bbox center is further LEFT (smaller x center), Costume B to the other. For top/bottom or front/back compositions apply the same idea sensibly (A to the upper/front one).
  - Scene has 1 character: apply Costume A only.
  - Scene has 3+ characters: Costume A to the most prominent character, Costume B to the second; the rest keep the prose clothing."""

_COSTUME_ASSIGN_ONE = """
  - Scene has 1 character: apply Costume A.
  - Scene has 2 characters: apply Costume A to the more prominent character; the other keeps the prose clothing. Do not put the same outfit on both unless the reference explicitly describes a matching set.
  - Never invent an extra character to match a costume."""


def costume_directive_block(costume_a: str, costume_b: str, mode: str = "自动") -> str:
    """服装参考 → 追加在拆解 system prompt 末尾的指令段。

    无服装输入或 mode=关闭 时返回 ""——prose_to_layout 据此保证不接入服装时
    system prompt 与历史版本（含仅姿势版本）逐字节一致。
    """
    if mode == "关闭":
        return ""
    a = (costume_a or "").strip()
    b = (costume_b or "").strip()
    if not a and not b:
        return ""
    lines = ""
    if a:
        lines += f"- Costume A: {a}\n"
    if b:
        lines += f"- Costume B: {b}\n"
    if a and b:
        assignment = _COSTUME_ASSIGN_BOTH
    else:
        # 只接了 costume_in_2：按单一参考（Costume B）处理
        assignment = _COSTUME_ASSIGN_ONE.replace("Costume A", "Costume B") if not a else _COSTUME_ASSIGN_ONE
    return _COSTUME_DIRECTIVE_TMPL.format(
        costume_lines=lines.rstrip("\n"), assignment=assignment, marker=COSTUME_MARKER)


def costume_category(description: str) -> str:
    """图库 description 的第一段（分类/季节）；手填文本无 " / " 时返回 ""。"""
    s = str(description or "")
    if " / " not in s:
        return ""
    return s.split(" / ", 1)[0].strip()


def costume_is_sensitive(description: str) -> bool:
    """是否为内衣/性感/泳装分类（云端 LLM/翻译前应提示内容审核风险）。"""
    return costume_category(description) in COSTUME_SENSITIVE_CATEGORIES


def _costume_phrase(description: str) -> str:
    """图库 description（分类 / 关键词1、关键词2…）→ 可写入 caption 的服装短语。

    季节/场合分类（春装/夏装/秋装/冬装/派对）只是图库标签，不拼进短语；
    泳装/内衣/性感保留分类作为语义锚点；其他未知分类（如手填"汉服 / …"）保留。
    格式不符（手填整句）时原样使用。只有分类没有关键词时，季节分类返回 ""
    （调用方视为无有效输入）。
    """
    # 先按原始串判断分隔符：先 strip 会把 "春装 / " 的尾空格去掉，导致分隔符失配
    s = str(description or "")
    if not s.strip():
        return ""
    if " / " not in s:
        return s.strip()
    category, _, rest = s.partition(" / ")
    category = category.strip()
    keywords = [p.strip() for p in rest.split("、") if p.strip()]
    if not keywords:
        if category and category not in _SEASONAL_COSTUME_CATEGORIES:
            return f"身穿{category}"
        return ""
    tags = "、".join(keywords)
    if category == "泳装":
        tags = f"泳装（{tags}）"
    elif category in _INTIMATE_COSTUME_CATEGORIES:
        tags = f"{category}风格服装（{tags}）"
    elif category and category not in _SEASONAL_COSTUME_CATEGORIES:
        tags = f"{category}（{tags}）"
    return f"身穿{tags}"


def _costume_set_caption(item: dict, description: str) -> None:
    """把服装短语写入角色 caption 的 ；服装： 段（幂等，保留姿势段/遮挡后缀）。"""
    phrase = _costume_phrase(description)
    if not phrase:
        raise ValueError("服装描述为空或只有季节分类，无法注入")
    _set_caption_segment(item, "costume", phrase)


def _bbox_overlap_ratio(inner, outer) -> float:
    """inner bbox 被 outer 覆盖的面积比例；非法 bbox 返回 0。"""
    try:
        ix0, iy0, ix1, iy1 = (float(v) for v in inner)
        ox0, oy0, ox1, oy1 = (float(v) for v in outer)
    except (TypeError, ValueError):
        return 0.0
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    area = iw * ih
    if area <= 0:
        return 0.0
    wx = max(0.0, min(ix1, ox1) - max(ix0, ox0))
    wy = max(0.0, min(iy1, oy1) - max(iy0, oy0))
    return (wx * wy) / area


def _strip_old_costume_objects(items: list, char_indices) -> int:
    """移除与目标角色重叠的旧服装 object 条目（反推草稿常见），返回移除数。

    只处理非 character、caption 命中服装名词、且 bbox 至少 30% 落在角色框内的
    条目；不重叠的场景服装（如橱窗里的裙子）与背景/配饰不受影响。
    label=text 的条目永不删除：画面可见文字（促销横幅等）即使命中服装名词
    （如"鞋类大促销"）也是用户内容，静默删除等于丢稿（v1.9.0-beta0 审查 P1-1）。
    """
    drop = set()
    for ci in char_indices:
        if ci < 0 or ci >= len(items) or not isinstance(items[ci], dict):
            continue
        cb = items[ci].get("bbox")
        for j, it in enumerate(items):
            if j == ci or j in drop or not isinstance(it, dict):
                continue
            if it.get("label") == "character" or it.get("label") == "text":
                continue
            cap = str(it.get("caption") or "")
            if not any(k in cap for k in _COSTUME_OBJECT_KEYWORDS):
                continue
            if _bbox_overlap_ratio(it.get("bbox"), cb) >= 0.30:
                drop.add(j)
    if drop:
        items[:] = [it for k, it in enumerate(items) if k not in drop]
    return len(drop)


def apply_costume_to_draft(draft: dict, costume_a: str, costume_b: str,
                           mode: str = "自动", target_index: int = -1) -> dict:
    """本地服装注入（📥服装按钮）：不调 LLM，把图库关键词短语写入角色 caption。

    分配规则（与 LLM 路径一致，服装没有"双人整体"语义）：
    - 指定 target_index：只注入该角色
    - 双参考：两人按 bbox x 中心分，左→costume_in、右→costume_in_2；
      3 人以上按面积排序，最大→costume_in、次大→costume_in_2
    - 单参考：给面积最大（最突出）的角色
    注入后立即 validate_draft；失败即抛错不写回。敏感分类只打印警告，不拦截。
    """
    if mode == "关闭":
        raise ValueError("costume_mode=关闭：请先切回 自动 再注入")
    # 不先 strip：_costume_phrase 需要原始 " / " 分隔符判断（"春装 / " 这类只有分类）
    raw_a = str(costume_a or "")
    raw_b = str(costume_b or "")
    a = _costume_phrase(raw_a)
    b = _costume_phrase(raw_b)
    if not a and not b:
        raise ValueError("未连接服装输入（costume_in / costume_in_2 为空或只有季节分类），无法注入")
    if costume_is_sensitive(raw_a) or costume_is_sensitive(raw_b):
        print("[MOSS Studio] 服装参考属于敏感分类（内衣/性感/泳装），"
              "请确认用途与内容合规", flush=True)
    if not isinstance(draft, dict) or not isinstance(draft.get("items"), list):
        raise ValueError("草稿缺少 items，不是拆解完成的布局形态")
    items = draft["items"]
    chars = [i for i, it in enumerate(items)
             if isinstance(it, dict) and it.get("label") == "character"]
    if not chars:
        raise ValueError("画布中没有 label=character 的角色条目，无法注入服装")

    assignments = []
    if isinstance(target_index, int) and target_index >= 0:
        if target_index >= len(items) or not isinstance(items[target_index], dict) \
                or items[target_index].get("label") != "character":
            raise ValueError(f"选中条目（index={target_index}）不是角色，请选中一个角色框再注入")
        assignments.append((target_index, a or b))
    else:
        by_area = sorted(chars, key=lambda i: _bbox_metrics(items[i])[1], reverse=True)
        if a and b:
            if len(chars) == 2:
                left, right = sorted(chars, key=lambda i: _bbox_metrics(items[i])[0])
                assignments += [(left, a), (right, b)]
            else:
                assignments.append((by_area[0], a))
                if len(by_area) > 1:
                    assignments.append((by_area[1], b))
        else:
            assignments.append((by_area[0], a or b))

    for idx, phrase in assignments:
        _set_caption_segment(items[idx], "costume", phrase)
    # 反推草稿常把旧服装拆成独立 object；不改它，模型会继续画旧衣服。
    removed = _strip_old_costume_objects(items, [idx for idx, _ in assignments])
    if removed:
        print(f"[MOSS Studio] 本地服装注入移除了 {removed} 个旧服装 object 条目", flush=True)

    validate_draft(draft)
    return draft
