"""翻译节点核心 + 子模块门面：结构完整性校验、翻译主流程与散文 AI 拆解主流程。

按职责拆出的子模块在下方统一 import（既供本文件主流程调用，也作为门面 re-export，
保证历史 `from py.translate import X` 用法不变）：
- llm_client：脱敏/Editor 源签名/provider 配置/端点解析/HTTP chat/JSON 修复提取
  （TranslationError / PermanentError / _plugin_root 定义地）
- agent_md：AGENT.md 指令文件体系
- caption_segments：caption 分段拆装；inject_pose / inject_costume：姿势/服装注入
- draft_repair：AI 拆解草稿本地修复

设计要点（总案确定）：
- 翻译只允许动 caption / meta / tags / palette 的描述性文字；
  text（画面可见文字）、bbox、label、键名、枚举、宽高必须逐字保留。
- 校验失败/HTTP 失败 → 把错误信息拼回对话重试（最多 2 次）→ 仍失败抛异常红字。
"""
from __future__ import annotations

import json
import re

try:
    from .compile_prompt import DraftError, VALID_CANVAS, VALID_LABELS, validate_draft
    from .sniff import strict_loads
except ImportError:  # 兼容非包方式导入
    from compile_prompt import DraftError, VALID_CANVAS, VALID_LABELS, validate_draft
    from sniff import strict_loads

# ---------- 拆分后子模块统一 import：主流程依赖 + 门面 re-export（零行为变化）----------

try:
    from .agent_md import (
        AGENT_BEGIN,
        AGENT_DEFAULT,
        AGENT_DIR,
        AGENT_END,
        AGENT_MD_FILENAME,
        AGENT_OFF,
        MAX_AGENT_FILE_BYTES,
        _agent_md_root_path,
        _read_agent_file,
        agent_md_widget_options,
        append_agent_md,
        list_agent_md_files,
        load_agent_md,
    )
    from .caption_segments import (
        COSTUME_MARKER,
        OCCL_BACK,
        OCCL_FRONT,
        POSE_MARKER,
        _CAPTION_SEGMENTS,
        _OCCL_SUFFIXES,
        _join_caption_segments,
        _set_caption_segment,
        _split_caption_segments,
    )
    from .draft_repair import (
        _AI_ITEM_KEYS,
        _AI_SUMMARY_KEYS,
        _AI_TOP_KEYS,
        _as_num,
        _forgive_llm_sloppiness,
        _repair_ai_draft,
    )
    from .inject_costume import (
        COSTUME_MODES,
        COSTUME_SENSITIVE_CATEGORIES,
        _COSTUME_ASSIGN_BOTH,
        _COSTUME_ASSIGN_ONE,
        _COSTUME_DIRECTIVE_TMPL,
        _COSTUME_OBJECT_KEYWORDS,
        _INTIMATE_COSTUME_CATEGORIES,
        _SEASONAL_COSTUME_CATEGORIES,
        _bbox_overlap_ratio,
        _costume_phrase,
        _costume_set_caption,
        _strip_old_costume_objects,
        apply_costume_to_draft,
        costume_category,
        costume_directive_block,
        costume_is_sensitive,
    )
    from .inject_pose import (
        POSE_MODES,
        _POSE_ASSIGN_BOTH,
        _POSE_ASSIGN_ONE,
        _POSE_DIRECTIVE_TMPL,
        _bbox_metrics,
        _pose_phrase,
        _pose_set_caption,
        apply_pose_to_draft,
        pose_directive_block,
    )
    from .llm_client import (
        PermanentError,
        TranslationError,
        _MAX_RESPONSE,
        _PROVIDER_DEFAULT_BASE,
        _llm_opener,
        _plugin_root,
        _repair_json_text,
        build_body,
        chat,
        config_key_allowed,
        extract_json,
        load_config,
        normalize_base_url,
        resolve_api_key,
        resolve_editor_text,
        resolve_endpoint,
        sanitize_secret,
        sanitize_url,
        source_signature,
    )
except ImportError:  # 兼容非包方式导入
    from agent_md import (
        AGENT_BEGIN,
        AGENT_DEFAULT,
        AGENT_DIR,
        AGENT_END,
        AGENT_MD_FILENAME,
        AGENT_OFF,
        MAX_AGENT_FILE_BYTES,
        _agent_md_root_path,
        _read_agent_file,
        agent_md_widget_options,
        append_agent_md,
        list_agent_md_files,
        load_agent_md,
    )
    from caption_segments import (
        COSTUME_MARKER,
        OCCL_BACK,
        OCCL_FRONT,
        POSE_MARKER,
        _CAPTION_SEGMENTS,
        _OCCL_SUFFIXES,
        _join_caption_segments,
        _set_caption_segment,
        _split_caption_segments,
    )
    from draft_repair import (
        _AI_ITEM_KEYS,
        _AI_SUMMARY_KEYS,
        _AI_TOP_KEYS,
        _as_num,
        _forgive_llm_sloppiness,
        _repair_ai_draft,
    )
    from inject_costume import (
        COSTUME_MODES,
        COSTUME_SENSITIVE_CATEGORIES,
        _COSTUME_ASSIGN_BOTH,
        _COSTUME_ASSIGN_ONE,
        _COSTUME_DIRECTIVE_TMPL,
        _COSTUME_OBJECT_KEYWORDS,
        _INTIMATE_COSTUME_CATEGORIES,
        _SEASONAL_COSTUME_CATEGORIES,
        _bbox_overlap_ratio,
        _costume_phrase,
        _costume_set_caption,
        _strip_old_costume_objects,
        apply_costume_to_draft,
        costume_category,
        costume_directive_block,
        costume_is_sensitive,
    )
    from inject_pose import (
        POSE_MODES,
        _POSE_ASSIGN_BOTH,
        _POSE_ASSIGN_ONE,
        _POSE_DIRECTIVE_TMPL,
        _bbox_metrics,
        _pose_phrase,
        _pose_set_caption,
        apply_pose_to_draft,
        pose_directive_block,
    )
    from llm_client import (
        PermanentError,
        TranslationError,
        _MAX_RESPONSE,
        _PROVIDER_DEFAULT_BASE,
        _llm_opener,
        _plugin_root,
        _repair_json_text,
        build_body,
        chat,
        config_key_allowed,
        extract_json,
        load_config,
        normalize_base_url,
        resolve_api_key,
        resolve_editor_text,
        resolve_endpoint,
        sanitize_secret,
        sanitize_url,
        source_signature,
    )

MAX_RETRY = 2  # 首次之外最多重试 2 次


DEFAULT_SYSTEM_PROMPT = """You are a translation engine for MOSS-Image prompts.
The input is a UTF-8 JSON prompt draft whose descriptive text is written in Chinese.
Output the SAME JSON with descriptive text translated into natural, vivid English suitable for an image-generation model.

Hard rules:
- Translate ONLY these string values: summary.caption, entries of summary.meta, each item's caption, and entries of item tags/palette.
- Keep VERBATIM, byte-for-byte, untranslated: every item "text" field (visible on-image wording), all keys, all "label" values, all numbers (width/height/bbox), bbox_coord_space, bbox_format, summary.canvas.
- Entries of any "palette" array that are hex color codes (like #F4EFE6 or #abc) must be copied EXACTLY unchanged; only translate palette entries that are descriptive color words.
- Do NOT add, remove, rename or reorder fields or items. The output must contain exactly the same items in the same order with identical bboxes.
- Do NOT add quality tags or style words that are absent in the source. Do not explain. Do not wrap the output in code fences.
- Inside JSON string values, never use unescaped double quotes: escape them as \\\" or prefer single quotes / no quotes.
- Translation style: concrete and visually descriptive image-prompt English; keep photography/art terminology accurate.

Output: the translated JSON object only, as raw JSON text."""


TOP_KEYS = ["width", "height", "bbox_coord_space", "bbox_format", "summary", "items"]
SUMMARY_KEYS = {"caption", "meta", "canvas", "palette"}
ITEM_KEYS = {"label", "caption", "bbox", "text", "tags", "palette"}


_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _check_hex_preserved(src_arr, out_arr, label: str):
    """palette 数组里的 hex 颜色必须逐字保留（大小写不敏感）；描述色名可正常翻译。"""
    for i, (a, b) in enumerate(zip(src_arr, out_arr)):
        if _HEX_RE.match(str(a).strip()):
            _require(str(b).strip().lower() == str(a).strip().lower(),
                     f"{label}[{i}] 十六进制颜色被改动: {a} -> {b}")


def _require(cond: bool, msg: str):
    if not cond:
        raise TranslationError(msg)


def check_translation(src_info: dict, out) -> dict:
    """结构完整性校验：翻译只准动描述性文字，其余必须与源逐字一致。"""
    kind = src_info["kind"]
    src = src_info["draft"]

    if kind == "caption":
        _require(isinstance(out, dict) and set(out) == {"caption"},
                 'caption 形态的输出必须恰好是 {"caption": "..."}')
        _require(isinstance(out["caption"], str) and out["caption"].strip(),
                 "输出 caption 为空")
        return out

    _require(isinstance(out, dict), "输出必须是 JSON 对象")
    _require(set(out) == set(TOP_KEYS),
             f"输出顶层字段与源不一致: {sorted(set(out) ^ set(TOP_KEYS))}")
    _require((out["width"], out["height"]) == (src["width"], src["height"]),
             f'宽高被改动: {src["width"]}x{src["height"]} -> {out["width"]}x{out["height"]}')
    _require(out["bbox_coord_space"] == "abs" and out["bbox_format"] == "xyxy",
             "输出坐标口径被改动（必须 abs/xyxy）")

    s_src, s_out = src["summary"], out["summary"]
    _require(isinstance(s_out, dict) and set(s_out) == set(s_src),
             "summary 字段集合与源不一致")
    _require(isinstance(s_out.get("caption"), str) and s_out["caption"].strip(),
             "summary.caption 为空")
    if "canvas" in s_src:
        _require(s_out["canvas"] == s_src["canvas"],
                 f'summary.canvas 枚举被改动: {s_src["canvas"]!r} -> {s_out["canvas"]!r}')
    for key in ("meta", "palette"):
        if key in s_src:
            _require(isinstance(s_out.get(key), list) and len(s_out[key]) == len(s_src[key]),
                     f"summary.{key} 数组长度与源不一致")
            if key == "palette":
                _check_hex_preserved(s_src[key], s_out[key], "summary.palette")

    items_src, items_out = src["items"], out["items"]
    _require(isinstance(items_out, list) and len(items_out) == len(items_src),
             f"items 数量不一致: {len(items_src)} -> {len(items_out)}")
    for i, (a, b) in enumerate(zip(items_src, items_out)):
        pre = f"items[{i}]"
        _require(isinstance(b, dict) and set(b) == set(a),
                 f"{pre} 字段集合与源不一致: {sorted(set(b) ^ set(a))}")
        _require(b["label"] == a["label"] and a["label"] in VALID_LABELS,
                 f'{pre}.label 被改动或非法')
        _require(isinstance(b["caption"], str) and b["caption"].strip(),
                 f"{pre}.caption 为空")
        _require(isinstance(b["bbox"], list) and len(b["bbox"]) == 4
                 and all(isinstance(x, (int, float)) for x in b["bbox"])
                 and b["bbox"] == a["bbox"],
                 f"{pre}.bbox 被改动: {a['bbox']} -> {b['bbox']}")
        if "text" in a:
            _require(b.get("text") == a["text"],
                     f'{pre}.text（画面可见文字）被改动，必须逐字保留:\n  源: {a["text"]!r}\n  译: {b.get("text")!r}')
        for key in ("tags", "palette"):
            if key in a:
                _require(isinstance(b.get(key), list) and len(b[key]) == len(a[key]),
                         f"{pre}.{key} 数组长度与源不一致")
                if key == "palette":
                    _check_hex_preserved(a[key], b[key], f"{pre}.palette")
    # 长度/hex 检查不够：翻译输出也要过一遍官方草稿校验（meta/tags/palette 必须是字符串数组等）
    try:
        validate_draft(out)
    except DraftError as e:
        raise TranslationError(f"翻译输出草稿不合法: {e}") from e
    return out


def translate(src_info: dict, *, base_url: str, api_key: str, body: dict,
              system_prompt: str, extra_instructions: str, timeout: int,
              agent_md: str = "") -> dict:
    """执行翻译（含重试循环）。src_info 来自 sniff()；body 来自 build_body()。"""
    src_json = json.dumps(src_info["draft"], ensure_ascii=False, indent=1)
    user = "Translate this MOSS-Image prompt draft JSON into English following the system rules:\n" + src_json
    if (extra_instructions or "").strip():
        user += "\n\nAdditional per-request instructions:\n" + extra_instructions.strip()

    base_messages = [
        {"role": "system", "content": append_agent_md(system_prompt, agent_md)},
        {"role": "user", "content": user},
    ]
    last_err = None
    last_raw = ""
    for attempt in range(1 + MAX_RETRY):
        body["messages"] = list(base_messages)
        try:
            raw = chat(base_url, api_key, body, timeout,
                       tag=f"translate attempt={attempt + 1}/{1 + MAX_RETRY}")
            last_raw = raw
            out = extract_json(raw)
            if isinstance(out, dict) and "meta" in out and isinstance(out.get("summary"), dict) \
                    and "meta" not in out["summary"]:
                # LLM 常见手误：把 summary.meta 写到顶层（与拆解路径的容错同类）；
                # 挪回 summary 后直接过校验，省一次无谓重试。
                out["summary"]["meta"] = out.pop("meta")
            return check_translation(src_info, out)
        except PermanentError:
            raise
        except Exception as e:  # 统一进入重试：HTTP 错误 / JSON 解析 / 结构校验
            safe_err = sanitize_secret(str(e), api_key, limit=300)
            print(f"[MOSS Studio] 翻译 attempt={attempt + 1} 失败: {safe_err}", flush=True)
            last_err = e
            if attempt < MAX_RETRY:
                base_messages = base_messages + [
                    {"role": "assistant", "content": last_raw[:2000]},
                    {"role": "user",
                     "content": f"上一次输出被拒绝：{safe_err}\n请修复以上问题，仅输出修正后的完整 JSON，不要任何解释。"},
                ]
    raise TranslationError(
        f"翻译失败（已重试 {MAX_RETRY} 次）: {sanitize_secret(str(last_err), api_key)}"
    )


DEFAULT_AI_DRAFT_PROMPT = """You convert an image prompt written as prose into a MOSS-Image layout draft JSON.
Input: the prose (usually Chinese) and the exact canvas size (WIDTH x HEIGHT).
Output: a single strict JSON object, raw JSON only, no code fences, no explanations.

Schema and rules:
- Top-level keys exactly: width, height, bbox_coord_space="abs", bbox_format="xyxy", summary, items.
- width and height are EXACTLY the given canvas size numbers.
- summary: {"caption": one-paragraph global scene description in Chinese (keep global style/lighting/mood and any negative requirements such as 无文字/无水印), "meta": 1-3 short English genre tags, "canvas": "single"}. When the scene has a clear color mood, also add "palette": 2-4 hex colors (e.g. "#F4EFE6").
- items: 1 to 6 positioned elements that make up the scene (main subjects, key foreground/background objects, any visible text). Do not fragment the scene into more than 6 boxes.
- Each item: {"label": "object"|"character"|"text", "caption": concrete visual description of that element in Chinese (appearance only; global style/lighting stays in summary), "bbox": [x_min, y_min, x_max, y_max]}.
- bbox: integer absolute pixels, top-left origin, 0 <= x_min < x_max <= width, 0 <= y_min < y_max <= height. Boxes describe placement, overlaps between elements are allowed.
- For every overlapping pair, append occlusion wording: to the FRONT item's caption append "，处于最前层，部分遮挡相邻元素", to the BACK item's caption append "，被前方元素部分遮挡".
- A "text" item must additionally carry "text": the exact visible characters in their original language. Do not create text items when the prose says there is no visible text.
- Use no fields beyond the schema above."""


def prose_to_layout(prose: str, *, width: int, height: int, base_url: str, api_key: str,
                    model: str, temperature: float, thinking: str, provider: str,
                    timeout: int, system_prompt: str = "", agent_md: str = "",
                    pose_a: str = "", pose_b: str = "", pose_mode: str = "自动",
                    costume_a: str = "", costume_b: str = "",
                    costume_mode: str = "自动") -> dict:
    """散文提示词 → 官方 layout 草稿 dict（含完整校验与带错重试）。

    pose_a/pose_b/pose_mode：可选姿势参考注入（如 Pose Gallery 的 description）。
    costume_a/costume_b/costume_mode：可选服装参考注入（如 Costume Gallery 的
    description）。无对应输入时指令段为空，system prompt 与历史版本逐字节一致；
    两者同时接入时顺序固定为姿势段 → 服装段。
    """
    sys_prompt = (system_prompt or "").strip() or DEFAULT_AI_DRAFT_PROMPT
    sys_prompt = append_agent_md(sys_prompt, agent_md)
    pose_block = pose_directive_block(pose_a, pose_b, pose_mode)
    if pose_block:
        sys_prompt = f"{sys_prompt.rstrip()}\n\n{pose_block}"
    costume_block = costume_directive_block(costume_a, costume_b, costume_mode)
    if costume_block:
        sys_prompt = f"{sys_prompt.rstrip()}\n\n{costume_block}"
    user = (f"Canvas size: {width} x {height}\n\nProse prompt:\n{prose}")
    body = build_body(model, temperature, thinking, provider, "")

    base_messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ]
    last_err = None
    last_raw = ""
    for attempt in range(1 + MAX_RETRY):
        body["messages"] = list(base_messages)
        try:
            raw = chat(base_url, api_key, body, timeout,
                       tag=f"draft attempt={attempt + 1}/{1 + MAX_RETRY}")
            last_raw = raw
            out = _forgive_llm_sloppiness(extract_json(raw))
            out = _repair_ai_draft(out, width, height, fallback_caption=prose)
            validate_draft(out)
            return out
        except PermanentError:
            raise
        except Exception as e:  # HTTP / JSON / 草稿校验 统一重试
            safe_err = sanitize_secret(str(e), api_key, limit=300)
            print(f"[MOSS Studio] AI 拆解 attempt={attempt + 1} 失败: {safe_err} "
                  f"| 输出开头: {last_raw[:160]!r}", flush=True)
            last_err = e
            if attempt < MAX_RETRY:
                base_messages = base_messages + [
                    {"role": "assistant", "content": last_raw[:2000]},
                    {"role": "user",
                     "content": f"上一次输出被拒绝：{safe_err}\n请修复以上问题，仅输出修正后的完整 layout 草稿 JSON。"},
                ]
    raise TranslationError(
        f"AI 拆解失败（已重试 {MAX_RETRY} 次）: {sanitize_secret(str(last_err), api_key)}"
    )
