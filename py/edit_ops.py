"""AI 定向改稿：ops schema 校验、本地应用器与改稿 LLM 提示词。

设计依据 MOSS-REVISION-RESEARCH-1101.md §5/§6（P1a-0 定案 + §6.10 评审勘误）：
- LLM 只输出操作列表（ops），不重写整稿；未被 op 引用的条目逐字节不动。
- target 指纹 {index,label,head_prefix} 永远对照原始草稿核验；不匹配拒收单条。
- 应用顺序：set_summary → item 级非 remove（列出顺序）→ remove（降序）→ add（追加）。
- ops ≤ MAX_EDIT_OPS：按列出顺序截断（重排之前——列出顺序即 LLM 优先级）。
- 指纹/scope 拒收是"合规但无 ops"，不触发重试；schema/validate 失败才带错重试。
"""
from __future__ import annotations

import copy
import json
import math

try:
    from .caption_segments import (
        COSTUME_MARKER,
        OCCL_BACK,
        OCCL_FRONT,
        POSE_MARKER,
        _join_caption_segments,
        _set_caption_segment,
        _split_caption_segments,
    )
    from .compile_prompt import (
        DraftError,
        ITEM_KEYS,
        SUMMARY_KEYS,
        VALID_CANVAS,
        VALID_LABELS,
        validate_draft,
    )
    from .llm_client import (
        PermanentError,
        TranslationError,
        build_body,
        chat,
        extract_json,
        sanitize_secret,
    )
except ImportError:  # 兼容非包方式导入（旧版 ComfyUI 加载器）
    from caption_segments import (
        COSTUME_MARKER,
        OCCL_BACK,
        OCCL_FRONT,
        POSE_MARKER,
        _join_caption_segments,
        _set_caption_segment,
        _split_caption_segments,
    )
    from compile_prompt import (
        DraftError,
        ITEM_KEYS,
        SUMMARY_KEYS,
        VALID_CANVAS,
        VALID_LABELS,
        validate_draft,
    )
    from llm_client import (
        PermanentError,
        TranslationError,
        build_body,
        chat,
        extract_json,
        sanitize_secret,
    )

try:
    from .agent_md import append_agent_md
except ImportError:  # 兼容非包方式导入
    from agent_md import append_agent_md

# 重试次数与 translate.MAX_RETRY 保持一致（translate 门面 import 本模块会造成
# 循环依赖，故本地持有；test_edit_ops 断言两者相等防漂移）。
MAX_RETRY = 2


# ---------- 常量（前端 js 镜像 edit_ops.js 的对拍目标，勿改名） ----------

EDIT_OP_TYPES = ("set_caption", "set_segment", "set_text", "move_resize",
                 "add_item", "remove_item", "set_summary")
EDIT_SEGMENT_KEYS = ("pose", "costume")
MAX_EDIT_OPS = 8
TARGET_KEYS = {"index", "label", "head_prefix"}
_OP_REQUIRED = {
    "set_caption": ("target", "head"),
    "set_segment": ("target", "segment", "text"),
    "set_text": ("target", "text"),
    "move_resize": ("target", "bbox"),
    "add_item": ("item",),
    "remove_item": ("target",),
    "set_summary": ("fields",),
}
_HEAD_FORBIDDEN = (POSE_MARKER, COSTUME_MARKER, OCCL_FRONT, OCCL_BACK)


class EditApplyError(ValueError):
    """应用 ops 后的草稿未通过官方 validate_draft（整单失败，可带错重试）。"""


class EditOpsError(ValueError):
    """LLM 输出不是合法的 ops 载荷（顶层结构错误，可带错重试）。"""


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _head_of(item: dict) -> str:
    head, _, _ = _split_caption_segments(item.get("caption") or "")
    return head


def parse_edit_scope(text):
    """edit_scope widget 文本 → {"indexes":[...], "summary":bool} 或 None（空/全稿）。

    非法 JSON / 结构不符直接抛错（改稿请求前置失败，不静默降级为全稿）。
    """
    s = str(text or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"edit_scope 不是合法 JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("edit_scope 必须是 {\"indexes\":[...],\"summary\":bool} 对象")
    extra = set(obj) - {"indexes", "summary"}
    if extra:
        raise ValueError(f"edit_scope 含未知字段: {sorted(extra)}")
    idx = obj.get("indexes")
    if not isinstance(idx, list) or not idx or not all(_is_int(i) and i >= 0 for i in idx):
        raise ValueError("edit_scope.indexes 必须是非空的非负整数数组")
    summary = obj.get("summary", False)
    if not isinstance(summary, bool):
        raise ValueError("edit_scope.summary 必须是布尔值")
    return {"indexes": list(idx), "summary": summary}


def _check_str_list(v, name: str) -> str:
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        return f"{name} 必须是字符串数组（元素不能为空）"
    return ""


def _check_bbox_value(b, w, h, name: str) -> str:
    if not isinstance(b, list) or len(b) != 4 or not all(_is_num(x) for x in b):
        return f"{name} 必须是四个有限数字 [x_min,y_min,x_max,y_max]"
    x0, y0, x1, y1 = b
    if not (0 <= x0 < x1 <= w) or not (0 <= y0 < y1 <= h):
        return f"{name} 越界或翻转（画布 {w}x{h}）: {b}"
    return ""


def _check_target(t, orig_items, scope) -> str:
    if not isinstance(t, dict) or set(t) != TARGET_KEYS:
        return "target 必须恰含 index/label/head_prefix 三字段"
    if not _is_int(t["index"]) or not (0 <= t["index"] < len(orig_items)):
        return f"index={t['index']!r} 越界"
    it = orig_items[t["index"]]
    prefix = t["head_prefix"]
    if not isinstance(prefix, str) or len(prefix.strip()) < 2:
        return "head_prefix 至少 2 个字符"
    if t["label"] != it.get("label"):
        return f"label 不匹配（目标实为 {it.get('label')!r}）"
    if not _head_of(it).strip().startswith(prefix.strip()):
        return "head_prefix 与目标当前描述开头不符（索引漂移防护）"
    if scope is not None and t["index"] not in scope["indexes"]:
        return f"scope 限定，index={t['index']} 不在选中范围"
    return ""


def _check_op(op, orig_items, w, h, scope, removed) -> str:
    """op 级校验：返回拒收原因（空串=通过）。对照原始草稿（指纹语义，见 §6.2）。"""
    if not isinstance(op, dict):
        return "op 必须是对象"
    kind = op.get("op")
    if kind not in EDIT_OP_TYPES:
        return f"未知 op 类型: {kind!r}"
    required = _OP_REQUIRED[kind]
    unknown = set(op) - {"op", "reason"} - set(required)
    if unknown:
        return f"op 含未知字段: {sorted(unknown)}"
    missing = [k for k in required if k not in op]
    if missing:
        return f"缺少必填字段: {missing}"
    reason = op.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return "reason 必填（审批卡展示用）"

    if kind == "set_summary":
        if scope is not None and not scope["summary"]:
            return "scope 限定时不允许修改 summary"
        f = op["fields"]
        if not isinstance(f, dict) or not f:
            return "fields 必须是非空对象"
        extra = set(f) - SUMMARY_KEYS
        if extra:
            return f"fields 含未知字段: {sorted(extra)}"
        if "caption" in f and not (isinstance(f["caption"], str) and f["caption"].strip()):
            return "fields.caption 不能为空"
        if "canvas" in f and f["canvas"] not in VALID_CANVAS:
            return f"fields.canvas 必须是 {sorted(VALID_CANVAS)} 之一"
        for key in ("meta", "palette"):
            if key in f:
                err = _check_str_list(f[key], f"fields.{key}")
                if err:
                    return err
        return ""

    if kind == "add_item":
        if scope is not None:
            return "scope 限定时不允许新增条目"
        item = op["item"]
        if not isinstance(item, dict):
            return "item 必须是对象"
        extra = set(item) - ITEM_KEYS
        if extra:
            return f"item 含未知字段: {sorted(extra)}"
        missing = {"label", "caption", "bbox"} - set(item)
        if missing:
            return f"item 缺少字段: {sorted(missing)}"
        if item["label"] not in VALID_LABELS:
            return f"item.label 必须是 {sorted(VALID_LABELS)} 之一"
        if not isinstance(item["caption"], str) or not item["caption"].strip():
            return "item.caption 不能为空"
        if item["label"] == "text":
            if not isinstance(item.get("text"), str) or not item["text"].strip():
                return "text 条目的 item.text 必填且不能为空"
        elif "text" in item and not (isinstance(item["text"], str) and item["text"].strip()):
            return "item.text 必须是非空字符串"
        for key in ("tags", "palette"):
            if key in item:
                err = _check_str_list(item[key], f"item.{key}")
                if err:
                    return err
        err = _check_bbox_value(item["bbox"], w, h, "item.bbox")
        if err:
            return err
        return ""

    # 以下都有 target
    err = _check_target(op["target"], orig_items, scope)
    if err:
        return err
    idx = op["target"]["index"]
    label = op["target"]["label"]

    if kind == "set_caption":
        head = op["head"]
        if not isinstance(head, str) or not head.strip():
            return "head 不能为空"
        for marker in _HEAD_FORBIDDEN:
            if marker in head:
                return f"head 不能含分段标记/遮挡后缀（{marker}…），分段会被误切"
        return ""
    if kind == "set_segment":
        if op["segment"] not in EDIT_SEGMENT_KEYS:
            return f"segment 必须是 {list(EDIT_SEGMENT_KEYS)} 之一"
        if label != "character":
            return "只有角色条目能设置姿势/服装分段"
        if not isinstance(op["text"], str) or not op["text"].strip():
            return "text 不能为空"
        return ""
    if kind == "set_text":
        if label != "text":
            return "只有文字条目能修改可见文字"
        if not isinstance(op["text"], str) or not op["text"].strip():
            return "text 不能为空"
        return ""
    if kind == "move_resize":
        return _check_bbox_value(op["bbox"], w, h, "bbox")
    if kind == "remove_item":
        if idx in removed:
            return f"重复删除同一目标 index={idx}"
        removed.add(idx)
        return ""
    return ""


def apply_ops(draft: dict, ops, scope=None) -> dict:
    """把 ops 应用到草稿副本：→ {"draft","applied","rejected","dropped"}。

    指纹/scope/字段校验失败 → 单条拒收进 rejected，不妨碍其他 op；
    应用完成后 validate_draft 闸门失败 → EditApplyError（整单，调用方决定重试）。
    全部拒收不报错（proposed == 原稿，合规但无 ops）。
    """
    w, h = validate_draft(draft)  # 输入本身必须合法
    if not isinstance(ops, list):
        raise EditOpsError("ops 必须是数组")
    dropped = max(0, len(ops) - MAX_EDIT_OPS)
    ops = ops[:MAX_EDIT_OPS]

    orig_items = draft["items"]
    work = copy.deepcopy(draft)
    removed: set = set()
    applied, rejected = [], []

    summary_ops, item_ops, remove_ops, add_ops = [], [], [], []
    for op in ops:
        err = _check_op(op, orig_items, w, h, scope, removed)
        if err:
            rejected.append({"op": op, "reason": err})
            continue
        kind = op["op"]
        if kind == "set_summary":
            summary_ops.append(op)
        elif kind == "remove_item":
            remove_ops.append(op)
        elif kind == "add_item":
            add_ops.append(op)
        else:
            item_ops.append(op)

    # 1) summary
    for op in summary_ops:
        for k, v in op["fields"].items():
            work["summary"][k] = copy.deepcopy(v)
        applied.append(op)
    # 2) item 级非 remove（列出顺序，索引=原始索引；remove 未发生，位置未变）
    items = work["items"]
    for op in item_ops:
        idx = op["target"]["index"]
        it = items[idx]
        kind = op["op"]
        if kind == "set_caption":
            _, segs, tail = _split_caption_segments(it.get("caption") or "")
            it["caption"] = _join_caption_segments(op["head"], segs, tail)
        elif kind == "set_segment":
            _set_caption_segment(it, op["segment"], op["text"])
        elif kind == "set_text":
            it["text"] = op["text"]
        elif kind == "move_resize":
            it["bbox"] = list(op["bbox"])
        applied.append(op)
    # 3) remove（降序，无位移问题）
    for op in sorted(remove_ops, key=lambda o: o["target"]["index"], reverse=True):
        del items[op["target"]["index"]]
        applied.append(op)
    # 4) add（追加）
    for op in add_ops:
        items.append(copy.deepcopy(op["item"]))
        applied.append(op)

    try:
        validate_draft(work)
    except DraftError as e:
        raise EditApplyError(f"ops 应用后草稿未通过官方校验: {e}") from e
    return {"draft": work, "applied": applied, "rejected": rejected, "dropped": dropped}


def parse_ops_payload(obj) -> list:
    """LLM 顶层输出校验：只允许 {"ops":[...]}。失败抛 EditOpsError（可重试）。"""
    if not isinstance(obj, dict) or set(obj) != {"ops"}:
        raise EditOpsError('输出必须是恰好 {"ops":[...]} 的 JSON 对象')
    ops = obj["ops"]
    if not isinstance(ops, list):
        raise EditOpsError("ops 必须是数组")
    return ops


# ---------- 改稿 LLM ----------

DEFAULT_REVISE_PROMPT = """You revise an existing MOSS-Image layout draft by emitting a PATCH — a short list of operations. You NEVER rewrite or re-output the whole draft.
Input: the current draft JSON (an item's index is its 0-based position in the "items" array), the user's revision request (usually Chinese){scope_line}.
Output: a single strict JSON object {"ops":[...]} — raw JSON only, no code fences, no explanations.

Operation schema (every op carries a short Chinese "reason"):
- {"op":"set_caption","target":{...},"head":"new base description in Chinese"} — replaces ONLY the base description of one item; its pose/costume suffix segments and occlusion wording are preserved automatically by the host.
- {"op":"set_segment","target":{...},"segment":"pose"|"costume","text":"..."} — replaces one suffix segment of a character item.
- {"op":"set_text","target":{...},"text":"..."} — changes the literal visible text of a text item.
- {"op":"move_resize","target":{...},"bbox":[x_min,y_min,x_max,y_max]} — full new box, absolute pixels inside the canvas.
- {"op":"add_item","item":{"label":"object"|"character"|"text","caption":"...","bbox":[...]}} — appends a complete new item at the end.
- {"op":"remove_item","target":{...}} — deletes an item.
- {"op":"set_summary","fields":{"caption":"..." and/or "meta":[...],"canvas":"...","palette":[...]}} — updates only the given summary fields.
target = {"index": <item index>, "label": <its label>, "head_prefix": <the first ~10 characters of that item's base description — the caption text before any ；姿势：/；服装： segment — copied VERBATIM>}.

Rules:
- Minimal change: preserve unrequested content; only emit ops for elements or summary fields the user actually asked to change, plus strictly necessary consequences (e.g. occlusion wording when boxes start overlapping after a move).
- Captions describe the element AS IT WILL LOOK after the change — never "changed to"/"改为" style narration.
- Visible text changes only via set_text on a text item; keep punctuation and line breaks exact unless asked.
- Do not touch pose/costume segments unless asked (set_caption preserves them automatically).
- A moved/resized box must stay inside the canvas with positive width and height.
- At most 8 ops; if the request needs more, cover the most important parts."""

_SCOPE_LINE = (";\nSCOPE: only items {indexes} may be modified — "
               "no ops on other items, no summary changes, no new items")


def _scope_line(scope) -> str:
    if not scope:
        return ""
    return _SCOPE_LINE.format(indexes=scope["indexes"])


def revise_draft(draft: dict, request: str, scope, *, base_url: str, api_key: str,
                 model: str, temperature: float, thinking: str, provider: str,
                 timeout: int, system_prompt: str = "", agent_md: str = "") -> dict:
    """改稿主流程：当前草稿 + 修改要求 → LLM 产 ops → 本地应用。

    schema/EditOpsError/EditApplyError 失败带错重试（MAX_RETRY 同款）；
    PermanentError 直抛；指纹/scope 拒收不重试（合规但无 ops）。
    返回 {"draft","applied","rejected","dropped"}。
    """
    sys_prompt = (system_prompt or "").strip() or DEFAULT_REVISE_PROMPT
    sys_prompt = append_agent_md(sys_prompt, agent_md)
    user = ("Current draft:\n" + json.dumps(draft, ensure_ascii=False, indent=2)
            + "\n\nRevision request:\n" + request)
    body = build_body(model, temperature, thinking, provider, "")
    base_messages = [
        {"role": "system", "content": sys_prompt + _scope_line(scope)},
        {"role": "user", "content": user},
    ]
    last_err = None
    last_raw = ""
    for attempt in range(1 + MAX_RETRY):
        body["messages"] = list(base_messages)
        try:
            raw = chat(base_url, api_key, body, timeout,
                       tag=f"revise attempt={attempt + 1}/{1 + MAX_RETRY}")
            last_raw = raw
            ops = parse_ops_payload(extract_json(raw))
            return apply_ops(draft, ops, scope)
        except PermanentError:
            raise
        except Exception as e:  # HTTP / schema / validate 统一重试（拒收不在此列）
            safe_err = sanitize_secret(str(e), api_key, limit=300)
            print(f"[MOSS Studio] AI 改稿 attempt={attempt + 1} 失败: {safe_err} "
                  f"| 输出开头: {last_raw[:160]!r}", flush=True)
            last_err = e
            if attempt < MAX_RETRY:
                base_messages = base_messages + [
                    {"role": "assistant", "content": last_raw[:2000]},
                    {"role": "user",
                     "content": f"上一次输出被拒绝：{safe_err}\n请修复以上问题，仅输出修正后的 {{\"ops\":[...]}} JSON。"},
                ]
    raise TranslationError(
        f"AI 改稿失败（已重试 {MAX_RETRY} 次）: {sanitize_secret(str(last_err), api_key)}"
    )
