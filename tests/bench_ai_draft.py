"""AI 拆解（prose -> layout）对照实验。

真实调用配置里的 LLM，记录每次的耗时、尝试次数、输出 token 与是否成功。
用法（在插件根目录）：
    COMFY_MOSS_STUDIO_CONFIG=/path/to/config.json python tests/bench_ai_draft.py --reps 2

只做实验，不进入 tests/run_tests.py 的离线测试集。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py.translate import (  # noqa: E402
    DEFAULT_AI_DRAFT_PROMPT,
    POSE_MARKER,
    _forgive_llm_sloppiness,
    _llm_opener,
    build_body,
    extract_json,
    load_config,
    pose_directive_block,
    resolve_endpoint,
)
from py.compile_prompt import validate_draft  # noqa: E402

PROSE = (
    "特摄风格的战斗场景，9:16 竖构图。一位二十八岁的成年女性战士位于画面中央偏左，"
    "穿着银色与深蓝色相间的装甲，手持发光的长剑向前突进；右后方是一只巨大的机械怪兽，"
    "金属外壳上有橙色能量纹路，正抬起右臂砸下；左下角有一位受伤的男性队员半跪在地，"
    "伸手向前；背景是雨夜的城市废墟，霓虹灯反射在积水地面，冷蓝色主光，电影感，"
    "画面无文字、无水印。"
)
W, H = 928, 1648

NORM_BBOX_OLD = (
    "- bbox: integer absolute pixels, top-left origin, "
    "0 <= x_min < x_max <= width, 0 <= y_min < y_max <= height. "
    "Boxes describe placement, overlaps between elements are allowed."
)
NORM_BBOX_NEW = (
    "- bbox: four integers in 0..1000 relative coordinates, top-left origin, "
    "[x_min,y_min,x_max,y_max], with 0 <= x_min < x_max <= 1000 and "
    "0 <= y_min < y_max <= 1000. Do NOT output absolute pixels; the host scales "
    "them to the real canvas. Boxes describe placement, overlaps between elements are allowed."
)

SUMMARY_OLD = (
    '- summary: {"caption": one-paragraph global scene description in Chinese '
    '(keep global style/lighting/mood and any negative requirements such as 无文字/无水印), '
    '"meta": 1-3 short English genre tags, "canvas": "single"}. '
    'When the scene has a clear color mood, also add "palette": 2-4 hex colors (e.g. "#F4EFE6").'
)
SUMMARY_BUDGET = (
    '- summary: {"caption": one-paragraph global scene description in Chinese '
    '(keep global style/lighting/mood and any negative requirements such as 无文字/无水印), '
    '"meta": 1-2 short English genre tags, "canvas": "single"}. Omit "palette".'
)
ITEM_OLD = (
    '- Each item: {"label": "object"|"character"|"text", "caption": concrete visual '
    'description of that element in Chinese (appearance only; global style/lighting stays '
    'in summary), "bbox": [x_min, y_min, x_max, y_max]}.'
)
ITEM_BUDGET = (
    '- Each item: {"label": "object"|"character"|"text", "caption": one concise Chinese '
    'sentence describing that element (appearance only, at most 30 Chinese characters), '
    '"bbox": [x_min, y_min, x_max, y_max]}.'
)
ITEMS_OLD = (
    "- items: 1 to 6 positioned elements that make up the scene (main subjects, key "
    "foreground/background objects, any visible text). Do not fragment the scene into "
    "more than 6 boxes."
)
ITEMS_BUDGET = (
    "- items: 1 to 4 positioned elements that make up the scene (main subjects, key "
    "foreground/background objects, any visible text). Do not fragment the scene into "
    "more than 4 boxes."
)


def norm_prompt(base: str) -> str:
    assert NORM_BBOX_OLD in base
    return base.replace(NORM_BBOX_OLD, NORM_BBOX_NEW)


def budget_prompt(base: str) -> str:
    out = base
    for old, new in ((ITEMS_OLD, ITEMS_BUDGET), (SUMMARY_OLD, SUMMARY_BUDGET), (ITEM_OLD, ITEM_BUDGET)):
        assert old in out, old[:40]
        out = out.replace(old, new)
    return out


SUMMARY_BUDGET_PALETTE = (
    '- summary: {"caption": one-paragraph global scene description in Chinese '
    '(keep global style/lighting/mood and any negative requirements such as 无文字/无水印), '
    '"meta": 1-2 short English genre tags, "canvas": "single"}. '
    'When the scene has a clear color mood, also add "palette": 2-4 hex colors (e.g. "#F4EFE6").'
)
BUDGET_PALETTE_PROMPT = budget_prompt(DEFAULT_AI_DRAFT_PROMPT).replace(SUMMARY_BUDGET, SUMMARY_BUDGET_PALETTE)
COMBINED_PROMPT = budget_prompt(norm_prompt(DEFAULT_AI_DRAFT_PROMPT))

VARIANTS = {
    "base": {"prompt": DEFAULT_AI_DRAFT_PROMPT, "norm": False, "json": False, "temp": 0.3, "max_tokens": 16384},
    "norm": {"prompt": norm_prompt(DEFAULT_AI_DRAFT_PROMPT), "norm": True, "json": False, "temp": 0.3, "max_tokens": 16384},
    "json": {"prompt": DEFAULT_AI_DRAFT_PROMPT, "norm": False, "json": True, "temp": 0.3, "max_tokens": 16384},
    "budget": {"prompt": budget_prompt(DEFAULT_AI_DRAFT_PROMPT), "norm": False, "json": False, "temp": 0.3, "max_tokens": 16384},
    "json_budget": {"prompt": budget_prompt(DEFAULT_AI_DRAFT_PROMPT), "norm": False, "json": True, "temp": 0.3, "max_tokens": 16384},
    "json_budget_fast": {"prompt": budget_prompt(DEFAULT_AI_DRAFT_PROMPT), "norm": False, "json": True, "temp": 0.1, "max_tokens": 4096},
    "budget_palette": {"prompt": BUDGET_PALETTE_PROMPT, "norm": False, "json": False, "temp": 0.3, "max_tokens": 16384},
    "json_budget_palette": {"prompt": BUDGET_PALETTE_PROMPT, "norm": False, "json": True, "temp": 0.3, "max_tokens": 16384},
    "combined": {"prompt": COMBINED_PROMPT, "norm": True, "json": True, "temp": 0.1, "max_tokens": 4096},
}

# 姿势注入变体（v1.7.0）：验证 pose_directive_block 在真实 LLM 上的合规率。
# 姿势描述取自 comfyui-pose-gallery 的 description 输出格式（体系/分类/姿势名称/视角）。
PROSE_SOLO = (
    "日系写真的室内场景，3:4 竖构图。一位二十五岁的成年女性站在落地窗前，"
    "穿米白色针织长裙，长发披肩，气质安静；午后阳光透过纱帘洒在木地板上，"
    "整体色调温暖，画面无文字、无水印。"
)
PROSE_PAIR = (
    "都市情感剧的 rooftop 场景，16:9 横构图。左侧一位二十八岁的成年男性穿深灰西装，"
    "右侧一位二十六岁的成年女性穿酒红色连衣裙，两人有互动；背景是黄昏的城市天际线，"
    "暖金色主光，电影感，画面无文字、无水印。"
)
POSE_A_DESC = "全身单人・标准系 / 跪立 / 四肢着地・单腿后高抬 / 俯视"
POSE_B_DESC = "全身单人・标准系 / 站姿 / S形站姿・胯部侧顶 / 正面"
POSE_PAIR_DESC = "双人缠绵 / 立位贴身 / 面对面・抱起单膝 / 正面"


def _pose_prompt(pose_a: str, pose_b: str = "", mode: str = "自动") -> str:
    block = pose_directive_block(pose_a, pose_b, mode)
    assert block, "姿势指令段为空：检查输入与 mode"
    return f"{DEFAULT_AI_DRAFT_PROMPT}\n\n{block}"


VARIANTS.update({
    "pose_solo": {"prompt": _pose_prompt(POSE_A_DESC), "norm": False, "json": False,
                  "temp": 0.3, "max_tokens": 16384, "prose": PROSE_SOLO},
    "pose_dual": {"prompt": _pose_prompt(POSE_A_DESC, POSE_B_DESC), "norm": False, "json": False,
                  "temp": 0.3, "max_tokens": 16384, "prose": PROSE_PAIR},
    "pose_pair": {"prompt": _pose_prompt(POSE_PAIR_DESC), "norm": False, "json": False,
                  "temp": 0.3, "max_tokens": 16384, "prose": PROSE_PAIR},
})


def post_chat(url: str, key: str, body: dict, timeout: int):
    """和 py.translate.chat 同口径，但额外返回 usage 与 finish_reason。"""
    full = url.rstrip("/")
    if not full.endswith("/chat/completions"):
        full += "/chat/completions"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key and key != "EMPTY":
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(full, data=data, headers=headers, method="POST")
    t0 = time.perf_counter()
    with _llm_opener().open(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    dt = time.perf_counter() - t0
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    return content, choice.get("finish_reason"), (payload.get("usage") or {}), dt


def clamp(v: float, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(v))))


def norm_to_abs(draft: dict, w: int, h: int) -> dict:
    draft["width"] = w
    draft["height"] = h
    draft["bbox_coord_space"] = "abs"
    draft["bbox_format"] = "xyxy"
    for it in draft.get("items") or []:
        b = it.get("bbox")
        if not isinstance(b, list) or len(b) != 4:
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in b)
        except (TypeError, ValueError):
            continue
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        ax0 = clamp(x0 * w / 1000, 0, w - 1)
        ax1 = clamp(x1 * w / 1000, 0, w)
        ay0 = clamp(y0 * h / 1000, 0, h - 1)
        ay1 = clamp(y1 * h / 1000, 0, h)
        if ax1 <= ax0:
            ax1 = min(w, ax0 + 8)
        if ay1 <= ay0:
            ay1 = min(h, ay0 + 8)
        it["bbox"] = [ax0, ay0, ax1, ay1]
    return draft


def run_one(name: str, cfg: dict, url: str, model: str, key: str, rep: int, timeout: int,
            provider: str = "deepseek", thinking: str = "off") -> dict:
    v = VARIANTS[name]
    prose = v.get("prose", PROSE)
    user = f"Canvas size: {W} x {H}\n\nProse prompt:\n{prose}"
    messages = [
        {"role": "system", "content": v["prompt"]},
        {"role": "user", "content": user},
    ]
    started = time.perf_counter()
    attempts = []
    last_raw = ""
    ok = False
    error = ""
    for attempt in range(1, 4):
        # 与插件同一套 thinking 映射：deepseek off→none，mimo off→thinking.type=disabled
        body = build_body(model, v["temp"], thinking, provider, "")
        body["messages"] = list(messages)
        body["max_tokens"] = v["max_tokens"]
        if "max_completion_tokens" in body:
            body["max_completion_tokens"] = v["max_tokens"]
        if v["json"]:
            body["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        try:
            raw, finish, usage, _ = post_chat(url, key, body, timeout)
            last_raw = raw
            out = _forgive_llm_sloppiness(extract_json(raw))
            if v["norm"]:
                out = norm_to_abs(out, W, H)
            validate_draft(out)
            ok = True
            items = out.get("items") or []
            caps = [str(it.get("caption") or "") for it in items]
            attempts.append({
                "n": attempt, "seconds": round(time.perf_counter() - t0, 2), "ok": True,
                "finish": finish, "completion_tokens": usage.get("completion_tokens"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "items": len(items),
                "avg_caption_len": round(sum(len(c) for c in caps) / len(caps), 1) if caps else 0,
                "palette": bool((out.get("summary") or {}).get("palette")),
                "meta": bool((out.get("summary") or {}).get("meta")),
                # 姿势注入合规：带 ；姿势： 标记的角色条目数（非姿势变体应为 0）
                "pose_marked": sum(1 for it in items
                                   if it.get("label") == "character"
                                   and POSE_MARKER in str(it.get("caption") or "")),
            })
            break
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            attempts.append({
                "n": attempt, "seconds": round(time.perf_counter() - t0, 2), "ok": False,
                "error": error[:300],
            })
            if attempt < 3:
                messages += [
                    {"role": "assistant", "content": last_raw[:2000]},
                    {"role": "user", "content": f"上一次输出被拒绝：{e}\n请修复以上问题，仅输出修正后的完整 layout 草稿 JSON。"},
                ]
    total = round(time.perf_counter() - started, 2)
    return {
        "variant": name, "rep": rep, "ok": ok, "attempts": len(attempts),
        "seconds": total, "attempts_detail": attempts,
        "final_error": "" if ok else error[:300],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="base,norm,json,budget,combined")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--thinking", default="off", choices=["off", "low", "medium", "high"])
    ap.add_argument("--base-url", default="", help="留空读 config.json")
    ap.add_argument("--model", default="", help="留空读 config.json")
    ap.add_argument("--api-key", default="", help="留空读 config.json；不会打印")
    args = ap.parse_args()

    cfg = load_config()
    url, model = resolve_endpoint(args.provider, args.base_url, args.model, "off", cfg)
    key = (args.api_key or ((cfg.get(args.provider) or {}).get("api_key") or "")).strip()
    print(f"provider={args.provider} model={model} endpoint={url} key_set={bool(key)}", flush=True)

    results = []
    for name in [x.strip() for x in args.variants.split(",") if x.strip()]:
        if name not in VARIANTS:
            raise SystemExit(f"unknown variant: {name}")
        for rep in range(1, args.reps + 1):
            r = run_one(name, cfg, url, model, key, rep, args.timeout, args.provider, args.thinking)
            results.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)

    print("\n=== SUMMARY ===", flush=True)
    for name in [x.strip() for x in args.variants.split(",") if x.strip()]:
        rows = [r for r in results if r["variant"] == name]
        if not rows:
            continue
        ok = sum(1 for r in rows if r["ok"])
        avg_s = sum(r["seconds"] for r in rows) / len(rows)
        avg_att = sum(r["attempts"] for r in rows) / len(rows)
        toks = [d.get("completion_tokens") for r in rows for d in r["attempts_detail"] if d.get("ok")]
        avg_tok = (sum(t for t in toks if t) / len(toks)) if toks else 0
        print(f"{name:9s} ok={ok}/{len(rows)} avg={avg_s:.1f}s attempts={avg_att:.2f} completion_tokens={avg_tok:.0f}", flush=True)


if __name__ == "__main__":
    main()
