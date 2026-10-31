"""LLM 客户端：密钥/URL 脱敏、Editor 源签名、provider 配置、端点解析、
HTTP chat 调用与模型输出 JSON 修复提取（翻译/拆解/反推/探测共用）。"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from .sniff import strict_loads
except ImportError:  # 兼容非包方式导入
    from sniff import strict_loads

try:
    from .http_safe import SameHostRedirect
except ImportError:  # 兼容非包方式导入
    from http_safe import SameHostRedirect

# 主 LLM 通道响应体上限（与 vision_probe._MAX_RESPONSE 对齐）：防止恶意/故障端点
# 返回数 GB body 把 ComfyUI 进程内存打爆。超限部分直接截断，解析失败按坏响应报错。
_MAX_RESPONSE = 1_000_000


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
