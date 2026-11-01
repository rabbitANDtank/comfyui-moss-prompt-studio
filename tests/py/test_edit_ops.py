"""AI 定向改稿：ops 应用器（含共享黄金 fixture）、scope 解析、LLM 流程与节点分支。"""
from helpers import LAYOUT_ZH, MockHandler, _start_mock  # noqa: F401  (helpers 先导入以初始化 sys.path)

import copy
import json
import math
import os

from py.edit_ops import (  # noqa: F401
    EDIT_OP_TYPES,
    EDIT_SEGMENT_KEYS,
    MAX_EDIT_OPS,
    EditApplyError,
    EditOpsError,
    apply_ops,
    parse_edit_scope,
    parse_ops_payload,
    revise_draft,
)
from py.translate import MAX_RETRY as TRANSLATE_MAX_RETRY, source_signature  # noqa: F401
from nodes.editor import MOSSEditor  # noqa: F401

_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures", "edit_ops_cases.json")


def _load_cases():
    with open(_FIXTURES, encoding="utf-8") as f:
        return json.load(f)


def test_edit_ops_fixture_cases():
    """共享黄金 fixture（前端 edit_ops.js 镜像将读同一文件做双端断言）。"""
    for case in _load_cases():
        name = case["name"]
        out = apply_ops(copy.deepcopy(case["draft"]), case["ops"], case.get("scope"))
        exp = case["expect"]
        assert len(out["applied"]) == exp["applied"], f"{name}: applied"
        assert len(out["rejected"]) == exp["rejected"], f"{name}: rejected"
        assert out["dropped"] == exp["dropped"], f"{name}: dropped"
        for key, want in exp.items():
            if key in ("applied", "rejected", "dropped"):
                continue
            if key == "proposed_equals_draft":
                assert out["draft"] == case["draft"], f"{name}: 全拒收时 proposed 应等于原稿"
                continue
            if key == "rejected0_reason_contains":
                assert want in out["rejected"][0]["reason"], f"{name}: 拒收理由应含 {want!r}"
                continue
            if key == "items_len":
                got = len(out["draft"]["items"])
            elif key.startswith("items"):
                idx_s, field = key.split("_", 1)
                got = out["draft"]["items"][int(idx_s[5:])][field]
            elif key.startswith("summary_"):
                got = out["draft"]["summary"][key.split("_", 1)[1]]
            else:
                raise AssertionError(f"{name}: 未知期望键 {key}")
            assert got == want, f"{name}: {key} got={got!r} want={want!r}"


def test_edit_ops_fingerprint_checked_against_original():
    """指纹永远对照原始草稿：set_caption 改了 head 之后，后续 set_segment 的
    head_prefix 仍按原稿核验（LLM 全部 op 都基于同一快照生成）。"""
    draft = {
        "width": 512, "height": 512, "bbox_coord_space": "abs", "bbox_format": "xyxy",
        "summary": {"caption": "场景。"},
        "items": [
            {"label": "character", "caption": "女战士站在中央；姿势：双手叉腰", "bbox": [64, 64, 448, 448]},
        ],
    }
    ops = [
        {"op": "set_caption", "target": {"index": 0, "label": "character", "head_prefix": "女战士站在中央"},
         "reason": "改外貌", "head": "白发女战士站在中央"},
        {"op": "set_segment", "target": {"index": 0, "label": "character", "head_prefix": "女战士站在中央"},
         "reason": "换姿势", "segment": "pose", "text": "单膝跪地"},
    ]
    out = apply_ops(draft, ops)
    assert len(out["applied"]) == 2 and not out["rejected"]
    assert out["draft"]["items"][0]["caption"] == "白发女战士站在中央；姿势：单膝跪地"


def test_edit_ops_head_with_marker_rejected():
    draft = {
        "width": 512, "height": 512, "bbox_coord_space": "abs", "bbox_format": "xyxy",
        "summary": {"caption": "场景。"},
        "items": [{"label": "object", "caption": "主体。", "bbox": [16, 16, 480, 480]}],
    }
    ops = [{"op": "set_caption", "target": {"index": 0, "label": "object", "head_prefix": "主体。"},
            "reason": "塞标记", "head": "新描述；姿势：单手托腮"}]
    out = apply_ops(draft, ops)
    assert len(out["rejected"]) == 1 and "分段标记" in out["rejected"][0]["reason"]


def test_edit_ops_validate_gate_raises():
    """删掉唯一条目 → items 为空 → 官方 validate_draft 拒绝 → 整单 EditApplyError。"""
    draft = {
        "width": 512, "height": 512, "bbox_coord_space": "abs", "bbox_format": "xyxy",
        "summary": {"caption": "场景。"},
        "items": [{"label": "object", "caption": "唯一主体。", "bbox": [16, 16, 480, 480]}],
    }
    ops = [{"op": "remove_item", "target": {"index": 0, "label": "object", "head_prefix": "唯一主体。"},
            "reason": "清空"}]
    try:
        apply_ops(draft, ops)
        raised = False
    except EditApplyError:
        raised = True
    assert raised


def test_parse_edit_scope():
    assert parse_edit_scope("") is None
    assert parse_edit_scope("   ") is None
    assert parse_edit_scope('{"indexes":[0,2],"summary":true}') == {"indexes": [0, 2], "summary": True}
    assert parse_edit_scope('{"indexes":[1]}') == {"indexes": [1], "summary": False}
    for bad in ("not json", "[]", "{}", '{"indexes":[]}', '{"indexes":[true]}',
                '{"indexes":[0],"extra":1}', '{"indexes":[0],"summary":"yes"}'):
        try:
            parse_edit_scope(bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, f"edit_scope={bad!r} 应报错"


def test_parse_ops_payload_strict():
    ops = parse_ops_payload({"ops": []})
    assert ops == []
    for bad in ({"ops": [], "note": 1}, {"op": []}, [1, 2], "x", {"ops": {}}):
        try:
            parse_ops_payload(bad)
            raised = False
        except EditOpsError:
            raised = True
        assert raised, f"payload={bad!r} 应报错"


def test_edit_ops_constants_pinned():
    """op 常量钉死（前端 edit_ops.js 镜像 + FNV 对拍的目标，P1a-2 接续）。"""
    assert EDIT_OP_TYPES == ("set_caption", "set_segment", "set_text", "move_resize",
                             "add_item", "remove_item", "set_summary")
    assert EDIT_SEGMENT_KEYS == ("pose", "costume")
    assert MAX_EDIT_OPS == 8
    import py.edit_ops as eo
    assert eo.MAX_RETRY == TRANSLATE_MAX_RETRY, "改稿重试数必须与 translate.MAX_RETRY 一致"
    # FNV 黄金值：与 tests/edit_ops.test.mjs 的跨语言对拍同一组字面量（P1a-2 双端契约）
    assert source_signature(",".join(EDIT_OP_TYPES)) == "fnv1a:a3f6b8de"
    assert source_signature(",".join(EDIT_SEGMENT_KEYS)) == "fnv1a:39f031be"


def _mk_revise_draft():
    return {
        "width": 1024, "height": 1024, "bbox_coord_space": "abs", "bbox_format": "xyxy",
        "summary": {"caption": "极简旅行海报，米色背景，蓝色山脉插画。", "meta": ["海报"], "canvas": "single"},
        "items": [
            {"label": "object", "caption": "下中部一座扁平蓝色山脉插画。", "bbox": [128, 384, 896, 896]},
            {"label": "text", "caption": "顶部深蓝色大写标题。", "text": "探索世界", "bbox": [256, 96, 768, 224]},
        ],
    }


def _revise_call(base_url, draft, ops_payloads, request="把标题换成「秋日市集」", scope=None):
    MockHandler.queue = list(ops_payloads)
    return revise_draft(draft, request, scope, base_url=base_url, api_key="EMPTY",
                        model="mock", temperature=0.3, thinking="off",
                        provider="custom", timeout=30)


def test_revise_draft_success():
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        ops = {"ops": [{"op": "set_text",
                        "target": {"index": 1, "label": "text", "head_prefix": "顶部深蓝色大写标题"},
                        "reason": "用户要求改标题", "text": "秋日市集"}]}
        out = _revise_call(f"http://127.0.0.1:{srv.server_address[1]}/v1", draft,
                           [json.dumps(ops, ensure_ascii=False)])
        assert MockHandler.hits == 1
        assert len(out["applied"]) == 1 and not out["rejected"]
        assert out["draft"]["items"][1]["text"] == "秋日市集"
        assert out["draft"]["items"][0]["caption"] == "下中部一座扁平蓝色山脉插画。", "未引用条目逐字节不动"
        sys_prompt = MockHandler.requests[-1]["messages"][0]["content"]
        assert "NEVER rewrite" in sys_prompt and "head_prefix" in sys_prompt
        user_msg = MockHandler.requests[-1]["messages"][1]["content"]
        assert "Revision request" in user_msg and "秋日市集" in user_msg
    finally:
        srv.shutdown()


def test_revise_draft_retries_on_bad_schema():
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        good = json.dumps({"ops": []}, ensure_ascii=False)
        out = _revise_call(f"http://127.0.0.1:{srv.server_address[1]}/v1", draft,
                           ["不是 JSON", good])
        assert MockHandler.hits == 2
        assert out["applied"] == [] and out["rejected"] == []
        # 重试消息带错误回灌
        msgs = MockHandler.requests[-1]["messages"]
        assert any("上一次输出被拒绝" in m.get("content", "") for m in msgs)
    finally:
        srv.shutdown()


def test_revise_draft_all_rejected_no_retry():
    """指纹拒收=合规但无 ops：不触发重试（与 schema 失败严格区分）。"""
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        ops = {"ops": [{"op": "set_text",
                        "target": {"index": 1, "label": "text", "head_prefix": "对不上的前缀"},
                        "reason": "改", "text": "X"}]}
        out = _revise_call(f"http://127.0.0.1:{srv.server_address[1]}/v1", draft,
                           [json.dumps(ops, ensure_ascii=False)])
        assert MockHandler.hits == 1, "全部拒收不应重试"
        assert out["applied"] == [] and len(out["rejected"]) == 1
        assert out["draft"] == draft
    finally:
        srv.shutdown()


def test_revise_draft_scope_line_in_prompt():
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        _revise_call(f"http://127.0.0.1:{srv.server_address[1]}/v1", draft,
                     [json.dumps({"ops": []}, ensure_ascii=False)],
                     scope={"indexes": [1], "summary": False})
        sys_prompt = MockHandler.requests[-1]["messages"][0]["content"]
        assert "SCOPE" in sys_prompt and "[1]" in sys_prompt
    finally:
        srv.shutdown()


def _run_editor_revise(base_url, source_text, edit_request="把标题换成「秋日市集」",
                       apply_edit=-2, edit_scope=""):
    return MOSSEditor().run(
        source_text, 1024, 1024, "",
        ai_draft=False, provider="custom", base_url=base_url, model="mock",
        api_key="EMPTY", temperature=0.3, thinking="off", timeout=30, ai_prompt="",
        edit_request=edit_request, edit_scope=edit_scope, apply_edit=apply_edit)


def test_editor_revision_branch_emits_mps_ops():
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        ops = {"ops": [{"op": "set_text",
                        "target": {"index": 1, "label": "text", "head_prefix": "顶部深蓝色大写标题"},
                        "reason": "改标题", "text": "秋日市集"}]}
        MockHandler.queue = [json.dumps(ops, ensure_ascii=False)]
        ret = _run_editor_revise(f"http://127.0.0.1:{srv.server_address[1]}/v1",
                                 json.dumps(draft, ensure_ascii=False))
        assert isinstance(ret, dict) and "ui" in ret
        payload = json.loads(ret["ui"]["mps_ops"])
        assert len(payload["ops"]) == 1 and payload["dropped"] == 0
        assert payload["request"] == "把标题换成「秋日市集」" and payload["scope"] is None
        assert payload["src_sig"] == source_signature(json.dumps(draft, ensure_ascii=False))
        assert payload["edits_sig"] == payload["src_sig"], "source 直连 layout 时两锚定一致"
        out_draft = json.loads(ret["result"][0])
        assert out_draft["items"][1]["text"] == "秋日市集"
        assert ret["result"][1:] == (1024, 1024)
        assert "mps_draft" not in ret["ui"], "改稿不占 mps_draft 通道（版本偏斜防护）"
    finally:
        srv.shutdown()


def test_editor_revision_requires_layout():
    try:
        _run_editor_revise("http://127.0.0.1:1/v1", "一段纯文本散文提示词。")
        raised = False
    except ValueError as e:
        raised = "layout" in str(e) or "拆解" in str(e)
    assert raised


def test_editor_revision_requires_request():
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        try:
            _run_editor_revise(f"http://127.0.0.1:{srv.server_address[1]}/v1",
                               json.dumps(draft, ensure_ascii=False), edit_request="  ")
            raised = False
        except ValueError as e:
            raised = "edit_request" in str(e)
        assert raised
        assert MockHandler.hits == 0
    finally:
        srv.shutdown()


def test_editor_revision_inert_when_off():
    """apply_edit=-1（默认/存档值）：edit_request 再长也不调 LLM、输出原稿。"""
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        ret = _run_editor_revise(f"http://127.0.0.1:{srv.server_address[1]}/v1",
                                 json.dumps(draft, ensure_ascii=False), apply_edit=-1)
        assert not isinstance(ret, dict), "无 ui 通道时应返回普通三元组"
        assert json.loads(ret[0]) == draft
        assert MockHandler.hits == 0
    finally:
        srv.shutdown()


def test_editor_revision_bool_apply_edit_ignored():
    """外部 API 传 JSON true：bool 是 int 子类，显式排除回落为不改稿。"""
    srv = _start_mock()
    try:
        draft = _mk_revise_draft()
        ret = MOSSEditor().run(
            json.dumps(draft, ensure_ascii=False), 1024, 1024, "",
            ai_draft=False, provider="custom",
            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", model="mock",
            api_key="EMPTY", temperature=0.3, thinking="off", timeout=30,
            edit_request="改标题", apply_edit=True)
        assert not isinstance(ret, dict) and MockHandler.hits == 0
    finally:
        srv.shutdown()


def test_is_changed_edit_bypasses_cache():
    src = json.dumps(_mk_revise_draft(), ensure_ascii=False)
    v = MOSSEditor.IS_CHANGED(src, 1024, 1024, "", ai_draft=False, apply_edit=-2)
    assert isinstance(v, float) and math.isnan(v), "apply_edit≠-1 必须绕过缓存"
    v2 = MOSSEditor.IS_CHANGED(src, 1024, 1024, "", ai_draft=False, apply_edit=-1)
    assert isinstance(v2, str) and v2, "默认路径仍走哈希缓存"
    # edit_request 进缓存键（照 ai_prompt 先例）
    v3 = MOSSEditor.IS_CHANGED(src, 1024, 1024, "", ai_draft=False,
                               edit_request="改A", apply_edit=-1)
    assert v3 != v2, "edit_request 变化应使缓存失效"


def test_editor_apply_edit_misaligned_string_normalized():
    """旧工作流 widgets_values 错位把 apply_edit 解成 ""：后端归一为 -1（不 400）。

    真机 2026-11-01 实测：v1.9.2 示例工作流（22 项 widgets_values）加载后
    edit_scope/apply_edit 错位解出 ""/edit_request 位置，直接 Queue 报
    "Failed to convert an input value to a INT"。前端归一化只覆盖加载后的
    widget 对象，本用例钉死后端兜底。
    """
    draft = _mk_revise_draft()
    ret = MOSSEditor().run(
        json.dumps(draft, ensure_ascii=False), 1024, 1024, "",
        ai_draft=False, apply_edit="")  # 错位值：字符串
    assert not isinstance(ret, dict), "归一为 -1 后走普通透传（无 ui 通道）"
    assert json.loads(ret[0]) == draft


def test_editor_apply_pose_misaligned_string_normalized():
    draft = _mk_revise_draft()
    ret = MOSSEditor().run(
        json.dumps(draft, ensure_ascii=False), 1024, 1024, "",
        ai_draft=False, apply_pose="")
    assert not isinstance(ret, dict)
    assert json.loads(ret[0]) == draft


def test_reason_missing_filled_not_rejected():
    """reason 纯展示字段：LLM 偶发漏字段时自动补默认文案，不拒收（真机 11-02 空卡根因）。"""
    draft = {
        "width": 512, "height": 512, "bbox_coord_space": "abs", "bbox_format": "xyxy",
        "summary": {"caption": "场景。", "meta": []},
        "items": [{"label": "object", "caption": "主体。", "bbox": [16, 16, 480, 480]}],
    }
    out = apply_ops(draft, [{"op": "set_summary", "fields": {"meta": ["x"]}}])
    assert len(out["applied"]) == 1 and not out["rejected"]
    assert out["applied"][0]["reason"] == "（LLM 未说明理由）"
