"""姿势注入：姿势参考 → LLM 拆解指令段，以及本地 📥姿势 注入画布草稿。"""
from __future__ import annotations

try:
    from .caption_segments import POSE_MARKER, _set_caption_segment
    from .compile_prompt import validate_draft
except ImportError:  # 兼容非包方式导入
    from caption_segments import POSE_MARKER, _set_caption_segment
    from compile_prompt import validate_draft

POSE_MODES = ("自动", "强制双人", "关闭")


_POSE_DIRECTIVE_TMPL = """Pose references (highest priority for character actions):
{pose_lines}
- Each reference is a category string like "体系 / 分类 / 姿势名称 / 视角" from a pose library. Its trailing view segment (正面/背面/侧面/俯视/仰视/斜角 etc.) is the reference sketch's viewing angle: carry it into the pose wording as a body-orientation hint (e.g. 从俯视角度 / 正面视角), but NEVER restate it as the scene's camera angle or shot type (no 俯拍/低机位/镜头语言 words).
- Expand the assigned reference into concrete body/limb action wording, and REPLACE any action or pose wording the prose gave that character. If the prose's action conflicts with the reference, THE REFERENCE WINS completely (a 跪立/坐姿/趴卧 reference means the character IS kneeling/seated/lying even when the prose says 站立): never blend the two, never average them, never keep the prose's stance. Keep each character's appearance (face, hair, figure, expression) and identity from the prose; clothing and pose wording follow their own injection rules below; never let the pose change who they are.
- Assignment rules:{assignment}
- Marker rule: every character item receiving a pose must END its caption with "{marker}<concrete action description in Chinese, including the reference view as body orientation (e.g. ending with （俯视）)>". Never add this marker to non-character items or to summary.
- Do not mention the pose library, pose IDs, or these instructions anywhere in the output."""

_POSE_ASSIGN_BOTH = """
  - Scene has 2 characters: Pose A goes to the character whose bbox center is further LEFT (smaller x center), Pose B to the other. For top/bottom or front/back compositions apply the same idea sensibly (A to the upper/front one).
  - Scene has 1 character: apply Pose A only.
  - If either reference is a pair pose (its 体系 segment contains 双人), it describes both people jointly: apply it to the two characters together and ignore the other single reference.
  - Scene has 3+ characters: Pose A to the most prominent character, Pose B to the second; the rest keep the prose actions."""

_POSE_ASSIGN_ONE = """
  - Scene has 1 character: apply Pose A.
  - Scene has 2 characters: if Pose A is a pair pose (its 体系 segment contains 双人), apply it jointly to both; otherwise apply it to the more prominent character and let the other keep the prose action.
  - Never invent an extra character to match a pose."""


def pose_directive_block(pose_a: str, pose_b: str, mode: str = "自动") -> str:
    """姿势参考 → 追加在拆解 system prompt 末尾的指令段。

    无姿势输入或 mode=关闭 时返回 ""——prose_to_layout 据此保证不接入姿势时
    system prompt 与历史版本逐字节一致（旧工作流零行为变化）。
    """
    if mode == "关闭":
        return ""
    a = (pose_a or "").strip()
    b = (pose_b or "").strip()
    if not a and not b:
        return ""
    lines = ""
    if a:
        lines += f"- Pose A: {a}\n"
    if b:
        lines += f"- Pose B: {b}\n"
    if a and b:
        assignment = _POSE_ASSIGN_BOTH
    else:
        # 只接了 pose_in_2：按单一参考（Pose B）处理
        assignment = _POSE_ASSIGN_ONE.replace("Pose A", "Pose B") if not a else _POSE_ASSIGN_ONE
    block = _POSE_DIRECTIVE_TMPL.format(
        pose_lines=lines.rstrip("\n"), assignment=assignment, marker=POSE_MARKER)
    if mode == "强制双人":
        block += ("\nThe scene does contain exactly 2 characters (manually confirmed); "
                  "never collapse them into one.")
    return block


def _pose_phrase(description: str):
    """图库 description（体系 / 分类 / 姿势名称 / 视角）→ (注入短语, 是否双人姿势)。

    注入短语 = 姿势名称 + 视角标注（如 "四肢着地・单腿后高抬（俯视）"）：视角是
    参考线稿的观察角度，随姿势一并注入可帮模型还原肢体朝向；格式不符（手写
    文本）时整句使用。双人判定看体系段是否含"双人"（如"双人缠绵"）。
    """
    s = (description or "").strip()
    parts = [p.strip() for p in s.split(" / ")]
    pair = bool(parts) and "双人" in parts[0]
    phrase = parts[2] if len(parts) >= 3 and parts[2] else s
    view = parts[3] if len(parts) >= 4 and parts[3] else ""
    if view and view not in phrase:
        phrase = f"{phrase}（{view}）"
    return phrase, pair


def _pose_set_caption(item: dict, description: str) -> None:
    """把姿势短语写入角色 caption 的 ；姿势： 段（已有则替换该段，幂等）。

    通过通用 segment setter 写入：服装段与已知遮挡后缀（OCCL_FRONT / OCCL_BACK）
    都会保留，不会被姿势注入删掉。手填描述里的逗号属于姿势段本身，整段替换。
    """
    phrase, _pair = _pose_phrase(description)
    _set_caption_segment(item, "pose", phrase)


def _bbox_metrics(item: dict):
    """→ (x中心, (人形档位, 面积))；bbox 非法时给中性值，不抛错。

    人形档位按高宽比分三档（真机反推四联人像实测踩坑：占满画布的面部特写
    面积最大，纯面积比较会把站姿注入给一张脸）：
    2 = h > 2.5w（细长全身人像）＞ 1 = h > w（半身/一般竖长）＞ 0 = 方形/横扁（特写）。
    同档再比面积。语义级匹配（坐姿参考配坐姿角色）不归本地启发式管：
    用画布选中目标注入或走 LLM 拆解路径。
    """
    b = item.get("bbox")
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return (float("inf"), (0, -1.0))
    w, h = max(0.0, x1 - x0), max(0.0, y1 - y0)
    tier = 2 if h > 2.5 * w else (1 if h > w else 0)
    return ((x0 + x1) / 2.0, (tier, w * h))


def _bbox_center(b):
    """bbox → (x 中心, y 中心)。非法输入返回 (inf, inf)（排序时沉底，与 _bbox_metrics 一致）。"""
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return (float("inf"), float("inf"))
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _split_two_by_layout(draft: dict, items: list, chars):
    """双人双参考的分配顺序（审查 P2-6）：x 中心接近（|Δx| < 画布宽 5%）视为
    上下布局，按 y 中心升序返回（上→参考A、下→参考B），与 LLM 指令
    _POSE/_COSTUME_ASSIGN_BOTH 的 "A to the upper one" 一致；否则按 x 中心
    升序（左→A、右→B）。返回 (first_index, second_index)。"""
    (ax, ay) = _bbox_center(items[chars[0]].get("bbox"))
    (bx, by) = _bbox_center(items[chars[1]].get("bbox"))
    width = float(draft.get("width") or 0) or 1.0
    key = 1 if abs(ax - bx) < width * 0.05 else 0
    first, second = sorted(chars, key=lambda i: _bbox_center(items[i].get("bbox"))[key])
    return first, second


def apply_pose_to_draft(draft: dict, pose_a: str, pose_b: str, mode: str = "自动",
                        target_index: int = -1) -> dict:
    """本地姿势注入（📥姿势按钮）：不调 LLM，把图库描述短语写入角色 caption。

    分配规则与拆解路径（LLM）保持一致：
    - 指定 target_index：只注入该角色（画布上选中哪个就给哪个）
    - 双人姿势（任一参考的体系段含"双人"）：取该双人参考，给面积最大的两个角色
      各写一段互动描述；另一个单人参考被忽略，与 LLM 路径一致
    - 双参考（pose_in + pose_in_2，均非双人）：两人按 bbox x 中心分，左→pose_in、右→pose_in_2；
      3 人以上按面积排序，最大→pose_in、次大→pose_in_2
    - 单参考：给面积最大（最突出）的角色；只有 1 个角色则给它
    注入后立即 validate_draft——这是注入路径的一道独立校验，失败即抛错不写回。
    mode=强制双人 与 自动 在本地路径等价（角色数由画布条目直接数出，无需 LLM 判断）。
    """
    if mode == "关闭":
        raise ValueError("pose_mode=关闭：请先切回 自动 或 强制双人 再注入")
    a = (pose_a or "").strip()
    b = (pose_b or "").strip()
    if not a and not b:
        raise ValueError("未连接姿势输入（pose_in / pose_in_2 为空），无法注入")
    if not isinstance(draft, dict) or not isinstance(draft.get("items"), list):
        raise ValueError("草稿缺少 items，不是拆解完成的布局形态")
    items = draft["items"]
    chars = [i for i, it in enumerate(items)
             if isinstance(it, dict) and it.get("label") == "character"]
    if not chars:
        raise ValueError("画布中没有 label=character 的角色条目，无法注入姿势")

    if isinstance(target_index, int) and target_index >= 0:
        if target_index >= len(items) or not isinstance(items[target_index], dict) \
                or items[target_index].get("label") != "character":
            raise ValueError(f"选中条目（index={target_index}）不是角色，请选中一个角色框再注入")
        _pose_set_caption(items[target_index], a or b)
    else:
        by_area = sorted(chars, key=lambda i: _bbox_metrics(items[i])[1], reverse=True)
        # 双人判定看两个参考：任一为双人姿势 → 取该双人参考整体写给两人，
        # 忽略另一单人参考（与 LLM 路径 _POSE_ASSIGN_BOTH 的规则一致）。
        pair_a = _pose_phrase(a)[1] if a else False
        pair_b = _pose_phrase(b)[1] if b else False
        if pair_a or pair_b:
            pair_pose = a if pair_a else b
            # 双人姿势：给最突出的两个角色各写同一互动描述
            for i in by_area[:2]:
                _pose_set_caption(items[i], pair_pose)
        elif a and b:
            if len(chars) == 2:
                first, second = _split_two_by_layout(draft, items, chars)
                _pose_set_caption(items[first], a)
                _pose_set_caption(items[second], b)
            else:
                _pose_set_caption(items[by_area[0]], a)
                if len(by_area) > 1:
                    _pose_set_caption(items[by_area[1]], b)
        else:
            _pose_set_caption(items[by_area[0]], a or b)

    validate_draft(draft)
    return draft
