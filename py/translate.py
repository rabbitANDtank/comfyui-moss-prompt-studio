"""翻译节点核心：OpenAI 兼容调用 + 结构完整性校验 + 带错误重试。

设计要点（总案确定）：
- 翻译只允许动 caption / meta / tags / palette 的描述性文字；
  text（画面可见文字）、bbox、label、键名、枚举、宽高必须逐字保留。
- 校验失败/HTTP 失败 → 把错误信息拼回对话重试（最多 2 次）→ 仍失败抛异常红字。
- thinking:
  - deepseek：off → reasoning_effort="none"（不显式关闭时 DeepSeek flash 默认仍会思考）；
    low/medium/high → low/high/max。
  - mimo：thinking.type 二值开关（off → disabled，其余 → enabled）。官方 mimo-v2.6-* 默认开启
    深度思考，所以 off 必须显式发；开启时 temperature/top_p 会被服务端强制忽略。
  - custom：非 off 时发 reasoning_effort；部署不认该参数就用 extra_body 兜底
    （最后合并、可覆盖一切）。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request

try:
    from .compile_prompt import VALID_CANVAS, VALID_LABELS, validate_draft
    from .sniff import strict_loads
except ImportError:  # 兼容非包方式导入
    from compile_prompt import VALID_CANVAS, VALID_LABELS, validate_draft
    from sniff import strict_loads

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


class TranslationError(ValueError):
    """翻译结果结构不合规或调用失败。"""


def _plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- AGENT.md 追加注入（语义移植自 Codex-X 的 Append 模式）----------
# 持久化的用户指令文件：内容包在托管块标记里，追加在基础系统提示词
# （默认契约 / widget 改写后的契约）之后，不替换任何现有内容。
# 文件来源二选一：插件根目录 AGENT.md（默认），或 agents/ 目录下由节点下拉选择的 .md。
# 用户可在文件里放 BEGIN/END 标记圈定"要注入的部分"（Codex-X 托管块同款防御）。
AGENT_MD_FILENAME = "AGENT.md"
AGENT_DIR = "agents"
AGENT_DEFAULT = "(默认 AGENT.md)"
AGENT_OFF = "(不注入)"
AGENT_BEGIN = "<!-- MOSS-STUDIO:AGENT:BEGIN -->"
AGENT_END = "<!-- MOSS-STUDIO:AGENT:END -->"


def _agent_md_root_path() -> str:
    env = os.environ.get("COMFY_MOSS_STUDIO_AGENT_MD")
    return env or os.path.join(_plugin_root(), AGENT_MD_FILENAME)


def list_agent_md_files() -> list:
    """列出 agents/ 目录下可下拉选择的 .md 文件（LoadImage 列图片的同款交互）。"""
    d = os.path.join(_plugin_root(), AGENT_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.lower().endswith(".md") and os.path.isfile(os.path.join(d, f)))


def agent_md_widget_options() -> list:
    """节点下拉框选项：默认根目录 / 关闭 / agents/ 下的文件。

    末尾带一个空串选项：旧工作流/自动恢复的前端可能存出 agent_md=''，
    校验器要求组合值必须在列表内，空串在列表里才能过（语义=默认）。
    """
    return [AGENT_DEFAULT, AGENT_OFF] + list_agent_md_files() + [""]


def _read_agent_file(path: str) -> str:
    """读单个指令文件并应用托管块标记规则；不存在返回 ""。"""
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise TranslationError(f"指令文件读取失败 [{path}]: {e}") from e
    nb, ne = text.count(AGENT_BEGIN), text.count(AGENT_END)
    if nb or ne:
        if nb != 1 or ne != 1 or text.index(AGENT_BEGIN) > text.index(AGENT_END):
            raise TranslationError(
                f"指令文件 [{os.path.basename(path)}] 中的 MOSS-STUDIO 托管标记不完整或重复："
                f"{AGENT_BEGIN} / {AGENT_END} 需各恰好出现一次且 BEGIN 在前"
            )
        text = text[text.index(AGENT_BEGIN) + len(AGENT_BEGIN): text.index(AGENT_END)]
    return text.strip()


def load_agent_md(selection: str = "") -> str:
    """按节点下拉选择解析注入来源（每次调用重读盘，改文件即刻生效）。

    - AGENT_OFF → 不注入
    - AGENT_DEFAULT / 空 → 插件根目录 AGENT.md（旧行为）
    - 其他 → agents/ 目录下同名文件（basename 防路径穿越；不存在则报错并列出可选项）
    """
    sel = (selection or "").strip()
    if sel == AGENT_OFF:
        return ""
    if not sel or sel == AGENT_DEFAULT:
        return _read_agent_file(_agent_md_root_path())
    name = os.path.basename(sel)  # 防路径穿越：只认文件名
    path = os.path.join(_plugin_root(), AGENT_DIR, name)
    if not os.path.isfile(path):
        raise TranslationError(
            f"指令文件不存在: agents/{name}（现有: {list_agent_md_files() or '无'}；"
            f"下拉列表在刷新页面后更新，如同 LoadImage 的新图片）"
        )
    return _read_agent_file(path)


def append_agent_md(system_prompt: str, selection: str = "") -> str:
    """把选定的指令文件内容以托管块形式追加在系统提示词之后（追加注入，非替换）。"""
    extra = load_agent_md(selection)
    if not extra:
        return system_prompt
    return f"{system_prompt.rstrip()}\n\n{AGENT_BEGIN}\n{extra}\n{AGENT_END}"


def load_config() -> dict:
    """读取插件根目录 config.json（可选）。格式见 config.example.json。"""
    path = os.environ.get("COMFY_MOSS_STUDIO_CONFIG") or os.path.join(_plugin_root(), "config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise TranslationError(f"config.json 读取失败: {e}") from e


def resolve_endpoint(provider: str, base_url: str, model: str, thinking: str, config: dict):
    """合并 widget 值 > config.json > provider 预设，返回 (base_url, model)。"""
    cfg = (config or {}).get(provider) or {}
    base_url = (base_url or "").strip() or (cfg.get("base_url") or "").strip()
    model = (model or "").strip() or (cfg.get("model") or "").strip()

    if provider == "deepseek":
        base_url = base_url or "https://api.deepseek.com/v1"
        # DeepSeek 现行阵容（deepseek-flash / deepseek-v4-pro）不再靠换模型名开思考，
        # 统一用默认模型 + reasoning_effort 参数（见 build_body）
        model = model or "deepseek-chat"  # 官方别名，当前映射到 flash
    if not base_url:
        raise TranslationError(
            f"provider={provider} 的 base_url 未配置：请在节点 widget 填写，"
            "或写入插件根目录 config.json"
        )
    if not model:
        raise TranslationError(f"provider={provider} 的 model 未配置")
    return base_url, model


def build_body(model: str, temperature: float, thinking: str, provider: str, extra_body: str) -> dict:
    """组装请求体（不含 messages）。extra_body 最后合并，可覆盖一切参数。"""
    body = {
        "model": model,
        "messages": None,  # 由 translate() 填充
        "temperature": float(temperature),
        "stream": False,
        # 显式给足输出预算：默认 4096 会被思考过程的推理 token 吃掉，导致正文被截断成非法 JSON
        "max_tokens": 16384,
    }
    if provider == "mimo":
        # MiMo 官方：mimo-v2.6-* 默认开启深度思考，thinking.type 是二值开关（没有档位）。
        # 不显式 disabled 时，即使节点选 off 模型也会先思考，拆解/翻译会慢数倍。
        body["thinking"] = {"type": "enabled" if thinking != "off" else "disabled"}
        # MiMo 用 max_completion_tokens 限制“思考 + 正文”的总长度；max_tokens 单独给不准
        body["max_completion_tokens"] = body["max_tokens"]
    elif provider == "deepseek":
        # DeepSeek 不传 effort 时默认仍会思考；off 必须显式 none 才是真的关闭
        # effort 档位为 low/high/max，无 medium
        body["reasoning_effort"] = "none" if thinking == "off" else {
            "low": "low", "medium": "high", "high": "max",
        }[thinking]
    elif thinking != "off":
        # custom：OpenAI 风格 reasoning_effort；部署不认这个参数时用 extra_body 覆盖
        body["reasoning_effort"] = thinking
    extra = (extra_body or "").strip()
    if extra:
        try:
            obj = json.loads(extra)
        except json.JSONDecodeError as e:
            raise TranslationError(f"extra_body 不是合法 JSON: {e}") from e
        if not isinstance(obj, dict):
            raise TranslationError("extra_body 必须是 JSON 对象")
        body.update(obj)
    return body


def _llm_opener():
    """LLM 请求默认绕过系统/环境代理直连。

    DeepSeek 与 MiMo 均为国内端点直连即可；ComfyUI 启动器若注入了代理变量而代理
    软件未运行，urllib 会报 10061 积极拒绝。config.json 顶层 "use_proxy": true 可改回走代理。
    """
    try:
        if load_config().get("use_proxy"):
            return urllib.request.build_opener()
    except Exception:
        pass
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def chat(base_url: str, api_key: str, body: dict, timeout: int, tag: str = "") -> str:
    """POST {base_url}/chat/completions，返回 message.content。"""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    host = url.split("//")[-1].split("/")[0]
    prefix = "[MOSS Studio] LLM" + (f" {tag}" if tag else "")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    key = (api_key or "").strip()
    if key and key != "EMPTY":
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    t0 = time.perf_counter()
    try:
        with _llm_opener().open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        print(f"{prefix} {time.perf_counter() - t0:.2f}s host={host} "
              f"model={body.get('model')} thinking={body.get('thinking')} "
              f"effort={body.get('reasoning_effort')} HTTP {e.code}", flush=True)
        raise TranslationError(f"HTTP {e.code} [{url}]: {detail}") from e
    except urllib.error.URLError as e:
        raise TranslationError(f"连接失败 [{url}]（检查 base_url/网络/服务是否在跑）: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise TranslationError(f"响应不是 JSON [{url}]: {e}") from e
    except TimeoutError as e:
        raise TranslationError(f"请求超时 [{url}]（>{timeout}s），可在 timeout widget 调大") from e
    try:
        msg = payload["choices"][0]["message"]
        content = msg.get("content") or ""
        finish = payload["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError) as e:
        # 保留原始异常上下文，同时把耗时和请求参数关键项打到 ComfyUI 控制台
        print(f"{prefix} {time.perf_counter() - t0:.2f}s host={host} "
              f"model={body.get('model')} thinking={body.get('thinking')} "
              f"effort={body.get('reasoning_effort')} 响应缺少 choices", flush=True)
        raise TranslationError(
            "响应缺少 choices[0].message.content: "
            + json.dumps(payload, ensure_ascii=False)[:300]
        ) from e
    usage = payload.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    print(f"{prefix} {time.perf_counter() - t0:.2f}s host={host} "
          f"model={body.get('model')} thinking={body.get('thinking')} "
          f"effort={body.get('reasoning_effort')} finish={finish} "
          f"completion={usage.get('completion_tokens')} "
          f"reasoning={reasoning if reasoning is not None else '-'}", flush=True)
    if not content.strip():
        raise TranslationError("模型返回了空 content")
    if finish == "length":
        raise TranslationError(f"输出被 max_tokens 截断（finish_reason=length），末尾 60 字: …{content[-60:]}")
    return content


def _repair_json_text(s: str) -> str:
    """尽力修复 LLM 输出的两类高频 JSON 手误（仅在严格解析失败后作为兜底调用）：

    1. 字符串值内部出现未转义双引号（如 caption: "a 20\" pipe" 被写成 "a 20" pipe"）。
       判定法：遇到引号时向后看第一个非空白字符，若不是 , : ] } 或结尾，
       说明字符串在此处并没有真的结束，把该引号转义后留在字符串内。
    2. 输出提前收笔，结尾缺 ] / }。按括号栈补齐。

    修复结果仍要过 strict_loads 与后续结构校验（bbox/text 逐字比对），
    所以修错只会解析失败，不会悄悄污染翻译结果。
    """
    out = []
    stack = []
    in_str = False
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                out.append(s[i:i + 2])
                i += 2
                continue
            if c == '"':
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                nxt = s[j] if j < n else ""
                if nxt in (",", ":", "]", "}") or nxt == "":
                    in_str = False
                    out.append('"')
                else:
                    out.append('\\"')
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
        elif c in "[{":
            stack.append(c)
            out.append(c)
        elif c in "]}":
            if stack:
                stack.pop()
            out.append(c)
        else:
            out.append(c)
        i += 1
    if in_str:
        out.append('"')
    while stack:
        out.append("]" if stack.pop() == "[" else "}")
    return "".join(out)


def extract_json(text: str):
    """从模型输出里抠出 JSON：剥 <think>、剥代码围栏、兜底取首尾大括号，
    仍失败时做一次自动修复（内文引号转义 / 补齐缺失的收尾括号）再解析。

    全部失败时附带末尾片段，让重试的纠错消息能看出是截断还是引号未转义。
    """
    t = re.sub(r"(?is)<think>.*?</think>", "", text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if m:
        t = m.group(1).strip()

    def _try(s):
        try:
            return strict_loads(s)
        except Exception as e:
            raise TranslationError(f"{e} | 输出末尾 80 字: …{s[-80:]}") from e

    best_err = None
    try:
        return _try(t)
    except Exception as e:
        best_err = e

    candidates = []
    if "{" in t and "}" in t:
        sub = t[t.index("{"): t.rindex("}") + 1]
        try:
            return _try(sub)
        except Exception as e:
            best_err = e
        candidates.append(sub)
    elif "{" in t:
        candidates.append(t[t.index("{"):])

    for cand in candidates:
        rep = _repair_json_text(cand)
        if rep != cand:
            try:
                return _try(rep)
            except Exception as e:
                best_err = e
    if best_err is not None:
        raise best_err
    raise TranslationError("模型输出中找不到 JSON 对象")


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
            return check_translation(src_info, out)
        except Exception as e:  # 统一进入重试：HTTP 错误 / JSON 解析 / 结构校验
            print(f"[MOSS Studio] 翻译 attempt={attempt + 1} 失败: {str(e)[:300]}", flush=True)
            last_err = e
            if attempt < MAX_RETRY:
                base_messages = base_messages + [
                    {"role": "assistant", "content": last_raw[:2000]},
                    {"role": "user",
                     "content": f"上一次输出被拒绝：{e}\n请修复以上问题，仅输出修正后的完整 JSON，不要任何解释。"},
                ]
    raise TranslationError(f"翻译失败（已重试 {MAX_RETRY} 次）: {last_err}")


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


def _forgive_llm_sloppiness(obj):
    """仅用于 AI 拆解产物：宽容 LLM 常见手误。

    - meta/tags/palette 被写成单个字符串 → 按逗号拆成数组（中英文逗号都认）
    - 数组元素过滤空白；归一后为空则删掉该可选字段（官方要求非空数组）
    """
    def coerce(container, key):
        v = container.get(key)
        if isinstance(v, str):
            parts = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()] if v.strip() else []
        elif isinstance(v, list):
            parts = [str(x).strip() for x in v if str(x).strip()]
        else:
            return
        if parts:
            container[key] = parts
        else:
            container.pop(key, None)

    if not isinstance(obj, dict):
        return obj
    s = obj.get("summary")
    if isinstance(s, dict):
        for k in ("meta", "palette"):
            coerce(s, k)
    items = obj.get("items")
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                for k in ("tags", "palette"):
                    coerce(it, k)
    return obj


_AI_TOP_KEYS = {"width", "height", "bbox_coord_space", "bbox_format", "summary", "items"}
_AI_SUMMARY_KEYS = {"caption", "meta", "canvas", "palette"}
_AI_ITEM_KEYS = {"label", "caption", "bbox", "text", "tags", "palette"}


def _as_num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) else None


def _repair_ai_draft(obj, width: int, height: int, fallback_caption: str = ""):
    """仅用于 AI 拆解：把模型的常见偏差就地修掉，避免为了一个坏框整轮重试。

    - 删除 schema 外的顶层/元素字段（模型常多加 text/type/description）
    - 宽高/坐标口径强制改成目标画布；模型给了别的宽高时按比例缩放 bbox
    - bbox 排序、缩放并夹取到画布内，保证 x0<x1、y0<y1
    - 非 text 元素误带的 text 字段删掉
    结构与必填字段（caption/label/text）仍交给官方校验；修不好就继续重试。
    """
    if not isinstance(obj, dict):
        return obj
    notes = []
    # 模型常把 summary 的字段直接放到顶层；先收好，删完多余字段后再重建 summary
    src_summary = obj.get("summary")
    stray = {k: obj.get(k) for k in _AI_SUMMARY_KEYS if k in obj}

    for k in list(obj):
        if k not in _AI_TOP_KEYS:
            obj.pop(k, None)
            notes.append(f"顶层.{k}")

    src_w, src_h = _as_num(obj.get("width")), _as_num(obj.get("height"))
    scale_x = width / src_w if src_w and src_w > 0 else 1.0
    scale_y = height / src_h if src_h and src_h > 0 else 1.0
    if src_w and src_h and (abs(src_w - width) > 1 or abs(src_h - height) > 1):
        notes.append(f"宽高 {src_w:g}x{src_h:g}→{width}x{height}")

    obj["width"], obj["height"] = int(width), int(height)
    obj["bbox_coord_space"], obj["bbox_format"] = "abs", "xyxy"

    s = obj.get("summary")
    if isinstance(s, dict):
        # 顶层散落的 caption/meta/canvas/palette 收回 summary
        for k, v in stray.items():
            if k not in s and v is not None:
                s[k] = v
                notes.append(f"summary.{k}←顶层")
        for k in list(s):
            if k not in _AI_SUMMARY_KEYS:
                s.pop(k, None)
                notes.append(f"summary.{k}")
        if fallback_caption and not str(s.get("caption") or "").strip():
            s["caption"] = fallback_caption.strip()
            notes.append("summary.caption 用原文兜底")
    else:
        # summary 整个缺失或不是对象：用顶层字段重建
        rebuilt = {}
        if isinstance(src_summary, str) and src_summary.strip():
            rebuilt["caption"] = src_summary.strip()
        for k, v in stray.items():
            if v is not None and k not in rebuilt:
                rebuilt[k] = v
        if not rebuilt.get("caption") and fallback_caption.strip():
            rebuilt["caption"] = fallback_caption.strip()
            notes.append("summary 用原文兜底")
        if rebuilt:
            obj["summary"] = rebuilt
            notes.append("summary 由顶层字段重建")

    items = obj.get("items")
    if isinstance(items, list):
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            for k in list(it):
                if k not in _AI_ITEM_KEYS:
                    it.pop(k, None)
                    notes.append(f"items[{i}].{k}")
            if it.get("label") != "text" and "text" in it:
                it.pop("text", None)
                notes.append(f"items[{i}].text")
            b = it.get("bbox")
            if not isinstance(b, list) or len(b) != 4:
                continue
            nums = [_as_num(v) for v in b]
            if any(v is None for v in nums):
                continue
            x0, y0, x1, y1 = nums
            x0, x1 = min(x0, x1) * scale_x, max(x0, x1) * scale_x
            y0, y1 = min(y0, y1) * scale_y, max(y0, y1) * scale_y
            ix0 = max(0, min(width - 1, int(round(x0))))
            ix1 = max(0, min(width, int(round(x1))))
            iy0 = max(0, min(height - 1, int(round(y0))))
            iy1 = max(0, min(height, int(round(y1))))
            if ix1 <= ix0:
                ix1 = min(width, ix0 + 8)
            if iy1 <= iy0:
                iy1 = min(height, iy0 + 8)
            nb = [ix0, iy0, ix1, iy1]
            if nb != b:
                notes.append(f"items[{i}].bbox")
            it["bbox"] = nb

    if notes:
        print("[MOSS Studio] AI 拆解已本地修复: " + ", ".join(notes[:8])
              + (" …" if len(notes) > 8 else ""), flush=True)
    return obj


def prose_to_layout(prose: str, *, width: int, height: int, base_url: str, api_key: str,
                    model: str, temperature: float, thinking: str, provider: str,
                    timeout: int, system_prompt: str = "", agent_md: str = "") -> dict:
    """散文提示词 → 官方 layout 草稿 dict（含完整校验与带错重试）。"""
    sys_prompt = (system_prompt or "").strip() or DEFAULT_AI_DRAFT_PROMPT
    user = (f"Canvas size: {width} x {height}\n\nProse prompt:\n{prose}")
    body = build_body(model, temperature, thinking, provider, "")

    base_messages = [
        {"role": "system", "content": append_agent_md(sys_prompt, agent_md)},
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
        except Exception as e:  # HTTP / JSON / 草稿校验 统一重试
            print(f"[MOSS Studio] AI 拆解 attempt={attempt + 1} 失败: {str(e)[:300]} "
                  f"| 输出开头: {last_raw[:160]!r}", flush=True)
            last_err = e
            if attempt < MAX_RETRY:
                base_messages = base_messages + [
                    {"role": "assistant", "content": last_raw[:2000]},
                    {"role": "user",
                     "content": f"上一次输出被拒绝：{e}\n请修复以上问题，仅输出修正后的完整 layout 草稿 JSON。"},
                ]
    raise TranslationError(f"AI 拆解失败（已重试 {MAX_RETRY} 次）: {last_err}")
