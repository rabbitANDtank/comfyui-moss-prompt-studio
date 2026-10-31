"""姿势组：拆解 LLM 路径指令段 / 本地 📥姿势 注入 / Editor 全链路与缓存失效。"""
from helpers import (LAYOUT_ZH, MockHandler, POSE_A, POSE_B, ROOT, _captions,  # noqa: F401
                     _last_system_prompt, _mk_pose_draft, _run_editor_ai,
                     _run_editor_pose, _start_mock)

import json
import os

from py.translate import (DEFAULT_AI_DRAFT_PROMPT, prose_to_layout,  # noqa: F401
                          source_signature)
from nodes.editor import MOSSEditor  # noqa: F401

# 兼容垫片：原单文件位于 tests/run_tests.py，本文件有用例函数体通过 __file__
# 两级上溯反推仓库根目录（sys.path 定位）；拆分到 tests/py/ 后为保持函数体
# 零改动，将模块级 __file__ 指回原路径（dirname(dirname(__file__)) == ROOT）。
__file__ = os.path.join(ROOT, "tests", "run_tests.py")


# ---------- 拆解 LLM 路径：system prompt 指令段 ----------


def test_pose_directive_block_modes():
    from py.translate import POSE_MARKER, pose_directive_block
    # 无输入 / 关闭 → 空串：不接入姿势时 system prompt 与历史版本逐字节一致
    assert pose_directive_block("", "", "自动") == ""
    assert pose_directive_block("  ", None, "自动") == ""
    assert pose_directive_block(POSE_A, POSE_B, "关闭") == ""
    both = pose_directive_block(POSE_A, POSE_B, "自动")
    assert POSE_A in both and POSE_B in both and POSE_MARKER in both
    assert "further LEFT" in both and "双人" in both, "双参考须含左右分配与双人姿势规则"
    assert "body-orientation" in both and "（俯视）" in both, "视角须随姿势段注入且不得转为镜头指令"
    assert "REFERENCE WINS" in both and "never blend" in both, "冲突时参考姿势须完胜，禁止与散文动作融合"
    only_a = pose_directive_block(POSE_A, "", "自动")
    assert POSE_A in only_a and POSE_B not in only_a and "more prominent" in only_a
    only_b = pose_directive_block("", POSE_B, "自动")
    assert POSE_B in only_b and "Pose B" in only_b, "只接 pose_in_2 时按单一参考 Pose B 处理"
    forced = pose_directive_block(POSE_A, POSE_B, "强制双人")
    assert "exactly 2 characters" in forced


def test_editor_pose_inputs_definition():
    opt = MOSSEditor.INPUT_TYPES()["optional"]
    # v1.7.1：pose_in/pose_in_2 为可点选/可手填 widget（不再 forceInput），连线时连线值优先
    assert not opt["pose_in"][1].get("forceInput") and not opt["pose_in_2"][1].get("forceInput")
    assert opt["pose_in"][1].get("multiline") and opt["pose_in"][1].get("default") == ""
    modes, meta = opt["pose_mode"]
    assert list(modes) == ["自动", "强制双人", "关闭"] and meta["default"] == "自动"


def test_apply_pose_top_bottom_layout_assigns_upper_first():
    """上下堆叠（x 中心接近）的双人双参考：A 给上面的、B 给下面的——与 LLM 指令一致
    （审查 P2-6：此前仅按 x 排序，x 相等时方向取决于 items 顺序）。"""
    from py.translate import apply_pose_to_draft
    d = _mk_pose_draft()
    d["items"] = [
        {"label": "character", "caption": "上面的人物", "bbox": [200, 100, 824, 500]},
        {"label": "character", "caption": "下面的人物", "bbox": [200, 520, 824, 920]},
    ]
    out = apply_pose_to_draft(d, "双手叉腰站立（正面）", "双手背在身后（背面）")
    caps = [(it["caption"], ) for it in out["items"] if it.get("label") == "character"]
    assert caps[0][0].endswith("；姿势：双手叉腰站立（正面）"), f"上面的角色应得 A: {caps[0]}"
    assert caps[1][0].endswith("；姿势：双手背在身后（背面）"), f"下面的角色应得 B: {caps[1]}"
    # 左右布局回归：x 中心差异大时仍按左→A、右→B
    d2 = _mk_pose_draft()
    out2 = apply_pose_to_draft(d2, "双手叉腰站立（正面）", "双手背在身后（背面）")
    caps2 = [(it["caption"]) for it in out2["items"] if it.get("label") == "character"]
    assert caps2[0].endswith("；姿势：双手叉腰站立（正面）"), "左侧角色应得 A（回归）"
    assert caps2[1].endswith("；姿势：双手背在身后（背面）"), "右侧角色应得 B（回归）"


def test_locate_pose_catalog():
    import sys, tempfile, os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from server_routes import locate_pose_catalog
    # 未安装 → None（不抛错）
    assert locate_pose_catalog(custom_nodes_dirs=[]) is None
    # 模拟已安装：custom_nodes/comfyui-pose-gallery/library/data/pose-zh.json
    with tempfile.TemporaryDirectory() as td:
        lib = _os.path.join(td, "comfyui-pose-gallery", "library", "data")
        _os.makedirs(lib)
        p = _os.path.join(lib, "pose-zh.json")
        open(p, "w", encoding="utf-8").write("[]")
        assert locate_pose_catalog(custom_nodes_dirs=[td]) == p


def test_prose_to_layout_pose_prompt_assembly():
    """无姿势 → system prompt 与历史逐字节一致；有姿势 → 末尾追加指令段。"""
    from py.translate import append_agent_md, pose_directive_block
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 3

        def call(**kw):
            prose_to_layout("一段散文。", width=1024, height=1024,
                            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                            api_key="EMPTY", model="m", temperature=0.3,
                            thinking="off", provider="custom", timeout=30, **kw)

        call()
        baseline = append_agent_md(DEFAULT_AI_DRAFT_PROMPT, "")
        assert _last_system_prompt() == baseline, "无姿势时 system prompt 不得有任何变化"

        call(pose_a=POSE_A, pose_b=POSE_B)
        sys_with_pose = _last_system_prompt()
        assert sys_with_pose == baseline + "\n\n" + pose_directive_block(POSE_A, POSE_B)
        assert POSE_A in sys_with_pose and POSE_B in sys_with_pose

        call(pose_a=POSE_A, pose_mode="关闭")
        assert _last_system_prompt() == baseline, "关闭档必须等价于未接入"
    finally:
        srv.shutdown()


def test_editor_run_passes_pose_to_llm():
    """Editor 节点全链路：pose_in/pose_in_2/pose_mode 正确送达拆解 LLM 的 system prompt。"""
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 2
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"

        def run_pose(**kw):
            return MOSSEditor().run(
                "一段特摄战斗的纯文本提示词。", 1024, 1024, "",
                ai_draft=True, provider="custom", base_url=url, model="mock",
                api_key="EMPTY", temperature=0.3, thinking="off", timeout=30, ai_prompt="",
                **kw,
            )

        ret = run_pose(pose_in=POSE_A, pose_in_2=POSE_B, pose_mode="自动")
        assert "mps_draft" in ret["ui"], "姿势路径不影响拆解写回"
        assert POSE_A in _last_system_prompt() and POSE_B in _last_system_prompt()

        run_pose(pose_in=POSE_A, pose_in_2=POSE_B, pose_mode="关闭")
        assert POSE_A not in _last_system_prompt(), "关闭档不得把姿势送进 LLM"
    finally:
        srv.shutdown()


def test_editor_is_changed_tracks_pose():
    base = dict(source="散文", canvas_width=1024, canvas_height=1024, edits="",
                ai_draft=True)
    h0 = MOSSEditor.IS_CHANGED(**base)
    assert MOSSEditor.IS_CHANGED(**base, pose_in=POSE_A) != h0, "换姿势必须让缓存失效"
    assert MOSSEditor.IS_CHANGED(**base, pose_in_2=POSE_B) != h0
    assert MOSSEditor.IS_CHANGED(**base, pose_mode="关闭") != h0
    # 旧工作流姿势参数缺省（kwargs 兜底）也不得抛错
    assert MOSSEditor.IS_CHANGED("散文", 1024, 1024, "", True)


# ---------- 本地注入（📥姿势 按钮，不调 LLM） ----------


POSE_PAIR_DESC = "双人缠绵 / 立位贴身 / 面对面・抱起单膝 / 正面"


def test_pose_phrase_parsing():
    from py.translate import _pose_phrase
    # 图库四段格式：姿势名称 + 视角标注；体系段判定双人
    assert _pose_phrase(POSE_A) == ("四肢着地・单腿后高抬（俯视）", False)
    assert _pose_phrase(POSE_PAIR_DESC) == ("面对面・抱起单膝（正面）", True)
    # 只有三段（缺视角）：不加括号
    assert _pose_phrase("全身单人・标准系 / 跪立 / 四肢着地") == ("四肢着地", False)
    # 手写文本（无 " / " 分段）：整句使用
    assert _pose_phrase("手持长剑向前突进") == ("手持长剑向前突进", False)
    assert _pose_phrase("") == ("", False)


def test_apply_pose_single_character_replace_not_append():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    draft = _mk_pose_draft()
    draft["items"] = draft["items"][:1] + [draft["items"][2]]  # 只留 1 角色 + 1 物体
    out = apply_pose_to_draft(draft, POSE_A, "")
    assert out["items"][0]["caption"].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in out["items"][1]["caption"], "非角色条目不得被注入"
    # 重复注入换姿势：替换而非叠加（幂等）
    out = apply_pose_to_draft(out, POSE_B, "")
    assert out["items"][0]["caption"].endswith(POSE_MARKER + "S形站姿・胯部侧顶（正面）")
    assert out["items"][0]["caption"].count(POSE_MARKER) == 1
    # 已有姿势段时保留段前原文
    assert out["items"][0]["caption"].startswith("左侧较小的人物；姿势：")


def test_apply_pose_dual_left_right_and_prominence():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    # 双参考：按 bbox x 中心分配（左→A、右→B），与面积大小无关
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_A, POSE_B)
    caps = _captions(d)
    assert caps[0].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）"), "左角色应得 pose_in"
    assert caps[1].endswith(POSE_MARKER + "S形站姿・胯部侧顶（正面）"), "右角色应得 pose_in_2"
    # 单参考：给面积最大（最突出）的角色，另一人保留原文
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_A, "")
    caps = _captions(d)
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in caps[0]


def test_apply_pose_pair_pose_hits_both():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_PAIR_DESC, "")
    caps = _captions(d)
    assert caps[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert caps[1].endswith(POSE_MARKER + "面对面・抱起单膝（正面）"), "双人姿势应两人各写一段互动描述"
    assert POSE_MARKER not in caps[2]
    # 只接 pose_in_2 的双人姿势同样生效
    d2 = apply_pose_to_draft(_mk_pose_draft(), "", POSE_PAIR_DESC)
    assert _captions(d2)[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    # pose_in 单人 + pose_in_2 双人：双人参考整体套用给两人，忽略单人参考
    d3 = apply_pose_to_draft(_mk_pose_draft(), POSE_A, POSE_PAIR_DESC)
    caps = _captions(d3)
    assert caps[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert caps[1].endswith(POSE_MARKER + "面对面・抱起单膝（正面）"), \
        "pose_in_2 是双人姿势时必须写给两个角色"
    assert "四肢着地" not in caps[0] and "四肢着地" not in caps[1], "单人参考应被忽略"
    # pose_in 双人 + pose_in_2 单人：仍两人整体使用双人参考
    d4 = apply_pose_to_draft(_mk_pose_draft(), POSE_PAIR_DESC, POSE_B)
    caps = _captions(d4)
    assert caps[0].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert caps[1].endswith(POSE_MARKER + "面对面・抱起单膝（正面）")
    assert "S形站姿" not in caps[0] and "S形站姿" not in caps[1]


def test_apply_pose_closeup_face_not_selected():
    """真机反推踩坑回归（四联人像）：面部特写框 [579,0,1024,1024]（445x1024，
    高宽比 2.3）面积 455k 全场最大，但三个全身框高宽比 5.5+ 更细长——
    单参考必须注入全身框而非占满画布的特写脸。"""
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = _mk_pose_draft()
    d["items"] = [
        {"label": "character", "caption": "正面全身站姿，红衣女子", "bbox": [31, 46, 195, 957]},   # 164x911 比5.6 档2
        {"label": "character", "caption": "面部大特写", "bbox": [579, 0, 1024, 1024]},             # 445x1024 比2.3 档1
    ]
    out = apply_pose_to_draft(d, POSE_A, "")
    caps = _captions(out)
    assert POSE_MARKER in caps[0], "细长全身框应被优先选中"
    assert POSE_MARKER not in caps[1], "面部特写不应获得站姿"


def test_apply_pose_all_closeup_falls_back_to_area():
    """全是特写（无更细长框）时同档退回纯面积最大，不失效。"""
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = _mk_pose_draft()
    d["items"] = [
        {"label": "character", "caption": "小特写", "bbox": [0, 0, 300, 600]},    # 300x600 比2.0
        {"label": "character", "caption": "大特写", "bbox": [400, 100, 900, 900]}, # 500x800 比1.6
    ]
    out = apply_pose_to_draft(d, POSE_A, "")
    # 两者都 <2.5 同为档1；小的反而更细长(档更高)？300x600 比2.0 仍档1，500x800 比1.6 档1
    # 同档比面积 → 大特写 400k 胜小特写 180k
    assert POSE_MARKER in _captions(out)[1]


def test_apply_pose_target_index_selection():
    from py.translate import POSE_MARKER, apply_pose_to_draft
    d = apply_pose_to_draft(_mk_pose_draft(), POSE_A, POSE_B, target_index=1)
    caps = _captions(d)
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in caps[0], "指定目标时其他角色不动"
    for bad in (2, 99):  # 物体下标 / 越界：明确报错
        try:
            apply_pose_to_draft(_mk_pose_draft(), POSE_A, "", target_index=bad)
            assert False, f"target_index={bad} 应报错"
        except ValueError:
            pass


def test_apply_pose_error_matrix():
    from py.translate import apply_pose_to_draft
    cases = [
        ("关闭档", dict(draft=_mk_pose_draft(), pose_a=POSE_A, pose_b="", mode="关闭")),
        ("无姿势输入", dict(draft=_mk_pose_draft(), pose_a="", pose_b="")),
        ("无角色条目", dict(draft={"items": [{"label": "object", "caption": "x", "bbox": [0, 0, 9, 9]}]},
                     pose_a=POSE_A, pose_b="")),
        ("非布局形态", dict(draft={"width": 1024}, pose_a=POSE_A, pose_b="")),
    ]
    for name, kw in cases:
        try:
            apply_pose_to_draft(**kw)
            assert False, f"{name} 应报错"
        except ValueError:
            pass
    # 独立校验闸门：注入前草稿本身不合法（bbox 缺失）→ 注入后 validate 拒绝
    bad = _mk_pose_draft()
    del bad["items"][0]["bbox"]
    try:
        apply_pose_to_draft(bad, POSE_A, "")
        assert False, "非法草稿应被注入路径的 validate_draft 拦下"
    except Exception:
        pass


def test_pose_reinject_preserves_caption_tail():
    """重复 📥姿势 只保留已知遮挡后缀，其余旧姿势段（含手填逗号描述）整段替换。"""
    from py.translate import (OCCL_BACK, OCCL_FRONT, POSE_MARKER, _pose_set_caption,
                              apply_pose_to_draft)
    draft = _mk_pose_draft()
    draft["items"] = draft["items"][:1] + [draft["items"][2]]  # 只留 1 角色 + 1 物体
    out = apply_pose_to_draft(draft, POSE_A, "")
    out["items"][0]["caption"] += OCCL_FRONT
    out = apply_pose_to_draft(out, POSE_B, "")
    cap = out["items"][0]["caption"]
    assert cap.endswith(OCCL_FRONT), "遮挡措辞不得被重复注入吞掉"
    assert "S形站姿・胯部侧顶（正面）" in cap and "四肢着地" not in cap, "姿势段必须已更新"
    assert cap.count(POSE_MARKER) == 1

    # OCCL_BACK 同样保留
    item = {"label": "character", "caption": "某人；姿势：跪立" + OCCL_BACK}
    _pose_set_caption(item, POSE_A)
    assert item["caption"] == "某人；姿势：四肢着地・单腿后高抬（俯视）" + OCCL_BACK

    # 手填描述自带中文逗号：旧片段（双手插兜）必须整段丢弃，不得残留
    item = {"label": "character", "caption": "某人"}
    _pose_set_caption(item, "站立，双手插兜")
    _pose_set_caption(item, POSE_A)
    assert "双手插兜" not in item["caption"], "旧手填描述的逗号尾段不得被当成遮挡措辞"
    assert item["caption"] == "某人；姿势：四肢着地・单腿后高抬（俯视）"
    assert item["caption"].count(POSE_MARKER) == 1

    # 旧段含逗号 + 已知遮挡后缀：保留后缀、丢弃旧动作片段
    item = {"label": "character", "caption": "某人；姿势：站立，双手插兜" + OCCL_FRONT}
    _pose_set_caption(item, POSE_B)
    assert item["caption"] == "某人；姿势：S形站姿・胯部侧顶（正面）" + OCCL_FRONT
    assert "双手插兜" not in item["caption"]

    # 无标记时行为不变
    item = {"label": "character", "caption": "某人"}
    _pose_set_caption(item, POSE_A)
    assert item["caption"] == "某人；姿势：四肢着地・单腿后高抬（俯视）"


# ---------- Editor 全链路：注入写回 / 反推关联 / 签名回传 ----------


def test_editor_run_apply_pose_writeback_and_errors():
    from py.translate import POSE_MARKER
    draft = _mk_pose_draft()
    # ① 画布 edits 持有布局（拆解写回 / 反推 canvas_zh 的形态）：自动分配注入 → ui.mps_draft 写回
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False), apply_pose=-2, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert json.loads(ret["result"][0])["items"][1]["caption"] == caps[1], "输出与写回一致"
    # ② 指定目标下标：只注入选中角色
    ret = _run_editor_pose(json.dumps(_mk_pose_draft(), ensure_ascii=False),
                           apply_pose=0, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[0].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    assert POSE_MARKER not in caps[1]
    # ③ apply_pose 缺省（-1）：普通 Queue 不注入、不产生 ui —— 三节点原行为零变化
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False), pose_in=POSE_A)
    assert isinstance(ret, tuple) and len(ret) == 3, "无注入时保持普通三元组返回"
    # ④ 输入是散文（如反推 prose_zh 刚写入 source、edits 旧签名失效）：注入须明确报错
    try:
        _run_editor_pose(json.dumps(draft, ensure_ascii=False), source="一段散文。",
                         sig=source_signature("一段散文。") + "x",  # 签名不匹配 → 以 source 为准
                         apply_pose=-2, pose_in=POSE_A)
        assert False, "caption 形态注入应报错引导用户先拆解"
    except ValueError as e:
        assert "拆解" in str(e)
    # ⑤ IS_CHANGED：apply_pose != -1 绕过缓存（保证写回必达），缺省走哈希路径
    assert MOSSEditor.IS_CHANGED("散文", 8, 8, "", False, apply_pose=-2) != \
        MOSSEditor.IS_CHANGED("散文", 8, 8, "", False)
    import math
    assert math.isnan(MOSSEditor.IS_CHANGED("散文", 8, 8, "", False, apply_pose=0))


def test_editor_apply_pose_after_reverse_flows():
    """与反推节点关联：反推两种产物形态（draft_zh→source / canvas_zh→edits）都可注入。"""
    from py.translate import POSE_MARKER
    draft = _mk_pose_draft()
    draft_json = json.dumps(draft, ensure_ascii=False)
    # 反推 draft_zh：layout 写入 source（ai_draft 已被前端关掉）→ 直接注入
    ret = MOSSEditor().run(draft_json, 1024, 1024, "", ai_draft=False,
                           apply_pose=-2, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")
    # 反推 canvas_zh：layout 写入 edits，source 仍是原散文且签名匹配 → 注入 edits 画布
    sig = source_signature("一段散文。")
    ret = MOSSEditor().run("一段散文。", 1024, 1024, draft_json, ai_draft=False,
                           edits_source_sig=sig, apply_pose=-2, pose_in=POSE_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(POSE_MARKER + "四肢着地・单腿后高抬（俯视）")


def test_editor_run_ui_carries_source_sig():
    """ui.mps_src_sig 回传本次执行实际使用的 source 签名（apply_pose + AI 拆解）。"""
    draft = _mk_pose_draft()
    # ① apply_pose 分支：source 为空、布局在 edits
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False),
                           apply_pose=-2, pose_in=POSE_A)
    assert "mps_draft" in ret["ui"]
    assert ret["ui"]["mps_src_sig"] == source_signature("")

    # ② apply_pose 分支：source 散文 + edits 签名匹配
    sig = source_signature("一段散文。")
    ret = MOSSEditor().run("一段散文。", 1024, 1024,
                           json.dumps(draft, ensure_ascii=False), ai_draft=False,
                           edits_source_sig=sig, apply_pose=-2, pose_in=POSE_A)
    assert ret["ui"]["mps_src_sig"] == sig

    # ③ 普通 layout 透传（无 mps_draft）保持原三元组返回
    ret = MOSSEditor().run(json.dumps(draft, ensure_ascii=False), 1024, 1024, "",
                           ai_draft=False)
    assert isinstance(ret, tuple) and len(ret) == 3

    # ③b source 首尾空白必须与前端 currentSourceSig 一样按 trim 后签名，避免误判 stale
    ret = MOSSEditor().run("  一段散文。\n", 1024, 1024,
                           json.dumps(draft, ensure_ascii=False), ai_draft=False,
                           edits_source_sig=sig, apply_pose=-2, pose_in=POSE_A)
    assert ret["ui"]["mps_src_sig"] == sig

    # ④ AI 拆解分支同样回传签名
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)]
        ret = _run_editor_ai(f"http://127.0.0.1:{srv.server_address[1]}/v1")
        assert "mps_draft" in ret["ui"]
        assert ret["ui"]["mps_src_sig"] == source_signature("一段特摄战斗的纯文本提示词。")
    finally:
        srv.shutdown()
