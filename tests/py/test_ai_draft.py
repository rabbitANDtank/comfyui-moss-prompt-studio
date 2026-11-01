"""编辑器 AI 拆解：成功写回 / 本地容错修复 / 重试 / 关闭与透传 / 换源重拆。"""
from helpers import CUP, LAYOUT_ZH, MockHandler, _run_editor_ai, _start_mock  # noqa: F401

import json

from py.translate import source_signature  # noqa: F401
from nodes.editor import MOSSEditor  # noqa: F401


def test_ai_draft_success_and_writeback():
    srv = _start_mock()
    try:
        MockHandler.queue = ["```json\n" + json.dumps(LAYOUT_ZH, ensure_ascii=False) + "\n```"]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert isinstance(ret, dict) and "ui" in ret and "result" in ret
        assert json.loads(ret["result"][0]) == LAYOUT_ZH and ret["result"][1:] == (1024, 1024)
        assert json.loads(ret["ui"]["mps_draft"]) == LAYOUT_ZH, "拆解结果必须随 ui 写回前端"
        assert MockHandler.hits == 1
    finally:
        srv.shutdown()


def test_ai_draft_repairs_bad_bbox_and_extra_fields():
    """AI 拆解专用修复：越界框夹取、删除 schema 外字段，不再为一处小错整轮重试。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        bad["items"][0]["bbox"] = [0, 0, 9999, 500]  # 越界 → 本地夹取
        bad["items"][0]["description"] = "extra"     # 元素级多余字段 → 删除
        bad["text"] = "oops"                         # 顶层多余字段 → 删除
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        out = json.loads(ret["result"][0])
        assert MockHandler.hits == 1, "可本地修复的偏差不应触发重试"
        assert out["items"][0]["bbox"] == [0, 0, 1024, 500]
        assert "text" not in out and "description" not in out["items"][0]
    finally:
        srv.shutdown()


def test_ai_draft_repairs_missing_summary():
    """模型把 caption/meta/canvas 放到顶层、summary 整个缺失 → 本地重建 summary。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        s = bad.pop("summary")
        bad["caption"] = s["caption"]
        for k in ("meta", "canvas", "palette"):
            if k in s:
                bad[k] = s[k]
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        out = json.loads(ret["result"][0])
        assert MockHandler.hits == 1, "summary 缺失应本地重建，不重试"
        assert out["summary"]["caption"] == s["caption"]
        for k in ("meta", "canvas", "palette"):
            if k in s:
                assert out["summary"][k] == s[k]
    finally:
        srv.shutdown()


def test_ai_draft_falls_back_to_prose_for_empty_caption():
    """summary 在、但 caption 为空 → 用原始散文兜底，不重试。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        bad["summary"]["caption"] = ""
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        out = json.loads(ret["result"][0])
        assert MockHandler.hits == 1
        assert out["summary"]["caption"] == "一段特摄战斗的纯文本提示词。"
    finally:
        srv.shutdown()


def test_ai_draft_retry_on_unrepairable():
    """真正修不好的错误（items 为空）仍要走重试。"""
    import copy

    srv = _start_mock()
    try:
        bad = copy.deepcopy(LAYOUT_ZH)
        bad["items"] = []  # 空 items 无法本地修复 → 校验拒绝
        MockHandler.queue = [json.dumps(bad, ensure_ascii=False), json.dumps(LAYOUT_ZH, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert json.loads(ret["result"][0]) == LAYOUT_ZH and MockHandler.hits == 2
    finally:
        srv.shutdown()


def test_ai_draft_disabled_and_layout_passthrough():
    # ai_draft 关闭：纯文本仍按 caption 走
    out, _, _ = MOSSEditor().run("纯文本。", 1024, 1024, "", ai_draft=False)
    assert json.loads(out) == {"caption": "纯文本。"}
    # 输入已是 layout：ai_draft 不触发 LLM、不产生 ui
    ret = MOSSEditor().run(json.dumps(CUP, ensure_ascii=False), 1024, 1024, "", ai_draft=True,
                           provider="custom", base_url="http://127.0.0.1:1/v1", model="x",
                           api_key="EMPTY", temperature=0.3, thinking="off", timeout=5, ai_prompt="")
    assert isinstance(ret, tuple) and json.loads(ret[0])["width"] == 1536 and ret[1:] == (1536, 1024)


# ---------- 换源 + 旧 edits：必须重新拆解新散文 ----------


def test_editor_stale_edits_ai_draft_uses_new_source():
    """换源 + 旧 layout edits + ai_draft：必须重新拆解新散文（不能用旧布局透传）。"""
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)]
        ret = MOSSEditor().run(
            "新的散文提示词。", 1024, 1024, json.dumps(LAYOUT_ZH, ensure_ascii=False),
            ai_draft=True, provider="custom",
            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", model="mock",
            api_key="EMPTY", temperature=0.3, thinking="off", timeout=10, ai_prompt="",
            edits_source_sig=source_signature("旧散文"),
        )
        assert isinstance(ret, dict) and "ui" in ret, "应以新散文触发 AI 拆解"
        assert MockHandler.hits == 1
        assert json.loads(ret["result"][0]) == LAYOUT_ZH
    finally:
        srv.shutdown()
