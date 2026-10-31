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
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from .compile_prompt import DraftError, VALID_CANVAS, VALID_LABELS, validate_draft
    from .sniff import strict_loads
except ImportError:  # 兼容非包方式导入
    from compile_prompt import DraftError, VALID_CANVAS, VALID_LABELS, validate_draft
    from sniff import strict_loads

try:
    from .http_safe import SameHostRedirect
except ImportError:  # 兼容非包方式导入
    from http_safe import SameHostRedirect

MAX_RETRY = 2  # 首次之外最多重试 2 次
# 主 LLM 通道响应体上限（与 vision_probe._MAX_RESPONSE 对齐）：防止恶意/故障端点
# 返回数 GB body 把 ComfyUI 进程内存打爆。超限部分直接截断，解析失败按坏响应报错。
_MAX_RESPONSE = 1_000_000

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


class PermanentError(TranslationError):
    """不可重试的调用错误（400/401/403/404/422 等参数或鉴权错误）。"""


# ---------- 密钥/URL 脱敏（节点、探测、日志共用同一套规则）----------

def sanitize_url(url: str) -> str:
    """去掉 URL 里的 userinfo 和 query，只保留 scheme/host[:port]/path。"""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return "***"
    host = p.hostname or ""
    try:
        if p.port:
            host += f":{p.port}"
    except ValueError:
        pass
    return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))


def sanitize_secret(text: str, key: str = "", limit: int = 500) -> str:
    """从任意文本中抹掉 API key、Bearer 凭证、URL userinfo 和 query。

    上游 401 响应经常回显提交的 key（如 "Incorrect API key provided: sk-..."），
    这些文本会进入控制台/错误弹窗/history，必须在所有出口统一脱敏。
    """
    t = str(text or "")
    k = (key or "").strip()
    if k and k != "EMPTY":
        t = t.replace(k, "***")
    # Authorization: Bearer xxx / Bearer xxx（key 已替换时同一规则兜底其它凭证）
    t = re.sub(r"(?i)\b(authorization\s*[:=]\s*bearer\s+)[^\s,;\"'}]+", r"\1***", t)
    t = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", "Bearer ***", t)
    # https://user:pass@host/... 与 ?api_key=... 查询串
    t = re.sub(r"(?i)\b(https?://)[^/\s@]+@", r"\1***@", t)
    t = re.sub(r"(?i)\b(https?://[^\s?#]+)\?[^\s#]*", r"\1?***", t)
    if limit and len(t) > limit:
        t = t[:limit] + "…"
    return t


# ---------- Editor 源签名：edits 只对写入时的那份 source 生效 ----------

def source_signature(text: str) -> str:
    """FNV-1a 32 位（UTF-8 bytes）；前端 web/js/editor/model.js 用同一算法。"""
    h = 0x811C9DC5
    for b in (text or "").encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"fnv1a:{h:08x}"


def resolve_editor_text(source: str, edits: str, edits_source_sig: str = "") -> str:
    """决定 Editor 实际解析哪份文本（普通 UI / 保存恢复 / API 一致）。

    - edits 为空：source
    - source 为空：edits（纯画布内容）
    - 两者都有：只有 edits 明确记录了“写入时的 source 签名”且与当前 source 一致
      才用 edits；签名缺失（旧工作流/无头调用）或已变化时以 source 为准，
      避免换源后旧画布永久遮住新散文。
    """
    src = (source or "").strip()
    ed = (edits or "").strip()
    if not ed:
        return src
    if not src:
        return ed
    sig = (edits_source_sig or "").strip()
    if not sig:
        return src
    return ed if sig == source_signature(src) else src


# ---------- provider 配置 key 的安全回退 ----------

_PROVIDER_DEFAULT_BASE = {
    "deepseek": "https://api.deepseek.com/v1",
    "minimax": "https://api.minimax.cn/v1",
}


def normalize_base_url(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return ""
    try:
        p = urllib.parse.urlsplit(s)
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    try:
        port = p.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    return urllib.parse.urlunsplit((p.scheme.lower(), netloc, p.path.rstrip("/"), "", ""))


def config_key_allowed(provider: str, base_url: str, config: dict) -> bool:
    """只有目标地址等于该 provider 的 config.json/官方默认地址时，才允许回退配置 key。

    widget/调用者显式填写的任意地址（攻击者地址、内网服务）一律不允许使用配置 key，
    防止把 config.json 里的真实 key 发送到调用者指定的地址。base_url 为空表示
    走 config/默认地址，允许。
    """
    requested = normalize_base_url(base_url)
    if not requested:
        return True
    cfg = (config or {}).get(provider) or {}
    allowed = [cfg.get("base_url"), _PROVIDER_DEFAULT_BASE.get(provider)]
    return any(requested == normalize_base_url(a) for a in allowed if a)


def resolve_api_key(provider: str, base_url: str, api_key: str, config: dict):
    """返回 (key, auth_src)。widget 显式 key 永远优先；配置 key 只回退到可信端点。"""
    key = (api_key or "").strip()
    if key not in ("", "EMPTY"):
        return key, "widget"
    cfg_key = (((config or {}).get(provider) or {}).get("api_key") or "").strip()
    if cfg_key and config_key_allowed(provider, base_url, config):
        return cfg_key, "config"
    return "", "none"


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
MAX_AGENT_FILE_BYTES = 64 * 1024


def _agent_md_root_path() -> str:
    env = os.environ.get("COMFY_MOSS_STUDIO_AGENT_MD")
    return env or os.path.join(_plugin_root(), AGENT_MD_FILENAME)


def list_agent_md_files() -> list:
    """列出 agents/ 目录下可下拉选择的 .md 文件（LoadImage 列图片的同款交互）。"""
    d = os.path.join(_plugin_root(), AGENT_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(
        f for f in os.listdir(d)
        if f.lower().endswith(".md")
        and os.path.isfile(os.path.join(d, f))
        and not os.path.islink(os.path.join(d, f))
    )


def agent_md_widget_options() -> list:
    """节点下拉框选项：默认根目录 / 关闭 / agents/ 下的文件。

    末尾带一个空串选项：旧工作流/自动恢复的前端可能存出 agent_md=''，
    校验器要求组合值必须在列表内，空串在列表里才能过（语义=默认）。
    """
    return [AGENT_DEFAULT, AGENT_OFF] + list_agent_md_files() + [""]


def _read_agent_file(path: str) -> str:
    """读单个指令文件并应用托管块标记规则；不存在返回 ""。

    安全约束：拒绝符号链接和非普通文件，并限制大小（防任意文件读取/超大文件塞进提示词）。
    """
    if not os.path.exists(path):
        return ""
    if os.path.islink(path):
        raise TranslationError(f"指令文件不能是符号链接: {path}")
    try:
        st = os.stat(path)
    except OSError as e:
        raise TranslationError(f"指令文件读取失败 [{path}]: {e}") from e
    if not stat.S_ISREG(st.st_mode):
        raise TranslationError(f"指令文件必须是普通文件: {path}")
    if st.st_size > MAX_AGENT_FILE_BYTES:
        raise TranslationError(
            f"指令文件过大 [{os.path.basename(path)}]: {st.st_size} 字节，"
            f"上限 {MAX_AGENT_FILE_BYTES} 字节"
        )
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_AGENT_FILE_BYTES + 1)
    except OSError as e:
        raise TranslationError(f"指令文件读取失败 [{path}]: {e}") from e
    if len(raw) > MAX_AGENT_FILE_BYTES:
        raise TranslationError(
            f"指令文件过大 [{os.path.basename(path)}]: 上限 {MAX_AGENT_FILE_BYTES} 字节"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
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
    base = os.path.realpath(os.path.join(_plugin_root(), AGENT_DIR))
    path = os.path.join(base, name)
    real = os.path.realpath(path)
    try:
        inside = os.path.commonpath([base, real]) == base
    except ValueError:
        inside = False
    if not inside or os.path.islink(path):
        raise TranslationError(f"指令文件必须位于 agents/ 目录内且不能是符号链接: {name}")
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
    if provider == "minimax":
        base_url = base_url or "https://api.minimax.cn/v1"
        model = model or "MiniMax-M3.1-Flash"
    if not base_url:
        raise TranslationError(
            f"provider={provider} 的 base_url 未配置：请在节点 widget 填写，"
            "或写入插件根目录 config.json"
        )
    if not model:
        raise TranslationError(f"provider={provider} 的 model 未配置")
    return base_url, model


def build_body(model: str, temperature: float, thinking: str, provider: str, extra_body: str) -> dict:
    """组装请求体（不含 messages）。extra_body 最后合并；stream 始终强制 False。"""
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
    elif provider == "minimax":
        # MiniMax M3.1 Flash 不能关闭思考：发 none/disabled 会 400。
        # off 映射到最低档 low；思考 token 与正文共用 max_completion_tokens。
        body.pop("max_tokens", None)
        body["max_completion_tokens"] = 65536
        body["reasoning_effort"] = {
            "off": "low", "low": "low", "medium": "medium", "high": "max",
        }.get(thinking, "low")
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
    # chat() 不支持 SSE；extra_body 写 stream=true 会让响应变成流式而解析失败。
    # 最后强制关闭，保证“可覆盖一切”不会把节点弄坏。
    body["stream"] = False
    return body


def _llm_opener():
    """LLM 请求默认绕过系统/环境代理直连。

    DeepSeek 与 MiMo 均为国内端点直连即可；ComfyUI 启动器若注入了代理变量而代理
    软件未运行，urllib 会报 10061 积极拒绝。config.json 顶层 "use_proxy": true 可改回走代理。

    无论是否走代理都挂 SameHostRedirect：urllib 默认会把 Authorization 原样带到
    302 目标主机，跨主机重定向必须在发送前拦下（同主机重定向保持可用）。
    """
    handlers = [SameHostRedirect()]
    try:
        if load_config().get("use_proxy"):
            return urllib.request.build_opener(*handlers)
    except Exception:
        pass
    handlers.insert(0, urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def chat(base_url: str, api_key: str, body: dict, timeout: int, tag: str = "") -> str:
    """POST {base_url}/chat/completions，返回 message.content。"""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    safe_url = sanitize_url(url)
    try:
        host = urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        host = "?"
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
            payload = json.loads(resp.read(_MAX_RESPONSE).decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = sanitize_secret(e.read(_MAX_RESPONSE).decode("utf-8", "replace"), key)
        print(f"{prefix} {time.perf_counter() - t0:.2f}s host={host} "
              f"model={body.get('model')} thinking={body.get('thinking')} "
              f"effort={body.get('reasoning_effort')} HTTP {e.code}", flush=True)
        err = sanitize_secret(f"HTTP {e.code} [{safe_url}]: {detail}", key, limit=2000)
        # 鉴权/参数/路径/校验错误重试也不会好；429/5xx/408 才值得重试
        if e.code in (408, 409, 425, 429) or e.code >= 500:
            raise TranslationError(err) from e
        raise PermanentError(err) from e
    except urllib.error.URLError as e:
        reason = sanitize_secret(str(e.reason), key)
        raise TranslationError(
            f"连接失败 [{safe_url}]（检查 base_url/网络/服务是否在跑）: {reason}"
        ) from e
    except json.JSONDecodeError as e:
        raise TranslationError(f"响应不是 JSON [{safe_url}]: {e}") from e
    except TimeoutError as e:
        raise TranslationError(
            f"请求超时 [{safe_url}]（>{timeout}s），可在 timeout widget 调大"
        ) from e
    # MiniMax 原生/兼容端点可能 HTTP 200 + base_resp.status_code 报业务错误
    if isinstance(payload, dict) and isinstance(payload.get("base_resp"), dict):
        br = payload["base_resp"]
        code = br.get("status_code")
        if code not in (0, None):
            print(f"{prefix} {time.perf_counter() - t0:.2f}s host={host} "
                  f"model={body.get('model')} 业务错误 {code}", flush=True)
            raise PermanentError(sanitize_secret(
                f"MiniMax 业务错误 {code}: {str(br.get('status_msg') or '').strip()}", key
            ))
    try:
        msg = payload["choices"][0]["message"]
        content = msg.get("content") or ""
        finish = payload["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError) as e:
        # 保留原始异常上下文，同时把耗时和请求参数关键项打到 ComfyUI 控制台
        print(f"{prefix} {time.perf_counter() - t0:.2f}s host={host} "
              f"model={body.get('model')} thinking={body.get('thinking')} "
              f"effort={body.get('reasoning_effort')} 响应缺少 choices", flush=True)
        raise TranslationError(sanitize_secret(
            "响应缺少 choices[0].message.content: "
            + json.dumps(payload, ensure_ascii=False)[:300], key
        )) from e
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
        raise TranslationError(sanitize_secret(
            f"输出被 max_tokens 截断（finish_reason=length），末尾 60 字: …{content[-60:]}", key
        ))
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


# 姿势/服装注入：拆解时以图库描述替代角色动作/服装措辞。与前端
# web/js/editor/pose.js 的同名常量保持一致（caption 末尾的分段标记，供画布
# "对调两人姿势/服装"交换用）。两段可以共存，顺序固定为 姿势 → 服装。
POSE_MARKER = "；姿势："
POSE_MODES = ("自动", "强制双人", "关闭")
COSTUME_MARKER = "；服装："
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

_POSE_DIRECTIVE_TMPL = """Pose references (highest priority for character actions):
{pose_lines}
- Each reference is a category string like "体系 / 分类 / 姿势名称 / 视角" from a pose library. Its trailing view segment (正面/背面/侧面/俯视/仰视/斜角 etc.) is the reference sketch's viewing angle: carry it into the pose wording as a body-orientation hint (e.g. 从俯视角度 / 正面视角), but NEVER restate it as the scene's camera angle or shot type (no 俯拍/低机位/镜头语言 words).
- Expand the assigned reference into concrete body/limb action wording, and REPLACE any action or pose wording the prose gave that character. If the prose's action conflicts with the reference, THE REFERENCE WINS completely (a 跪立/坐姿/趴卧 reference means the character IS kneeling/seated/lying even when the prose says 站立): never blend the two, never average them, never keep the prose's stance. Keep each character's appearance (clothing, face, hair, figure), expression and identity from the prose; never let the pose change who they are.
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
    """
    drop = set()
    for ci in char_indices:
        if ci < 0 or ci >= len(items) or not isinstance(items[ci], dict):
            continue
        cb = items[ci].get("bbox")
        for j, it in enumerate(items):
            if j == ci or j in drop or not isinstance(it, dict):
                continue
            if it.get("label") == "character":
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
                left, right = sorted(chars, key=lambda i: _bbox_metrics(items[i])[0])
                _pose_set_caption(items[left], a)
                _pose_set_caption(items[right], b)
            else:
                _pose_set_caption(items[by_area[0]], a)
                if len(by_area) > 1:
                    _pose_set_caption(items[by_area[1]], b)
        else:
            _pose_set_caption(items[by_area[0]], a or b)

    validate_draft(draft)
    return draft


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
