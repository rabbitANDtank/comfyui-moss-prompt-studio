"""服装组（v1.8）：指令段 / 短语解析 / 本地注入 / 与姿势共存 / Editor 全链路。"""
from helpers import (LAYOUT_ZH, MockHandler, POSE_A, POSE_B, _captions,  # noqa: F401
                     _last_system_prompt, _mk_pose_draft, _run_editor_pose,
                     _start_mock)

import json

from py.translate import (DEFAULT_AI_DRAFT_PROMPT, prose_to_layout,  # noqa: F401
                          source_signature)
from nodes.editor import MOSSEditor  # noqa: F401


COSTUME_A = "春装 / 象牙白、荷叶边、针织、牛仔布、迷你、半身裙、大衣、短袜、白色、运动鞋"
COSTUME_B = "夏装 / 黑色、吊带、棉、短裤、凉鞋"
COSTUME_SENSITIVE = "内衣 / 黑色、蕾丝、文胸"
COSTUME_A_PHRASE = "身穿象牙白、荷叶边、针织、牛仔布、迷你、半身裙、大衣、短袜、白色、运动鞋"
COSTUME_B_PHRASE = "身穿黑色、吊带、棉、短裤、凉鞋"


def test_costume_directive_block_modes():
    from py.translate import (COSTUME_MARKER, costume_category,
                              costume_directive_block, costume_is_sensitive)
    # 无输入 / 关闭 → 空串；不接入时 system prompt 与历史版本逐字节一致
    assert costume_directive_block("", "", "自动") == ""
    assert costume_directive_block("  ", None, "自动") == ""
    assert costume_directive_block(COSTUME_A, COSTUME_B, "关闭") == ""
    both = costume_directive_block(COSTUME_A, COSTUME_B, "自动")
    assert COSTUME_A in both and COSTUME_B in both and COSTUME_MARKER in both
    assert "further LEFT" in both, "双参考须含左右分配规则"
    assert "REFERENCE WINS" in both and "never blend" in both, "冲突时参考服装须完胜"
    only_a = costume_directive_block(COSTUME_A, "", "自动")
    assert COSTUME_A in only_a and COSTUME_B not in only_a and "more prominent" in only_a
    assert "matching set" in only_a, "单参考不得默认给两人穿同一套"
    only_b = costume_directive_block("", COSTUME_B, "自动")
    assert COSTUME_B in only_b and "Costume B" in only_b, "只接 costume_in_2 时按单一参考处理"
    sensitive = costume_directive_block(COSTUME_SENSITIVE, "", "自动")
    assert "neutral terms" in sensitive and "内衣" in sensitive
    assert costume_category(COSTUME_A) == "春装"
    assert costume_category("手填红色长裙") == ""
    assert costume_is_sensitive(COSTUME_SENSITIVE)
    assert costume_is_sensitive("泳装 / 白色、比基尼")
    assert not costume_is_sensitive(COSTUME_A)
    assert not costume_is_sensitive("手填红色长裙")


def test_costume_phrase_parsing():
    from py.translate import _costume_phrase
    assert _costume_phrase(COSTUME_A) == COSTUME_A_PHRASE
    assert "春装" not in _costume_phrase(COSTUME_A), "季节分类不是服装，不拼进短语"
    assert _costume_phrase(COSTUME_B) == COSTUME_B_PHRASE
    assert _costume_phrase("泳装 / 白色、比基尼、系带") == "身穿泳装（白色、比基尼、系带）"
    assert _costume_phrase("内衣 / 黑色、蕾丝") == "身穿内衣风格服装（黑色、蕾丝）"
    assert _costume_phrase("汉服 / 红色、长裙") == "身穿汉服（红色、长裙）", "未知分类保留"
    assert _costume_phrase("红色长裙") == "红色长裙", "手填整句原样使用"
    assert _costume_phrase("春装 / ") == ""
    assert _costume_phrase("") == ""
    assert _costume_phrase("泳装 / ") == "身穿泳装"


def test_split_caption_segments_generic():
    from py.translate import (COSTUME_MARKER, OCCL_BACK, POSE_MARKER,
                              _join_caption_segments, _split_caption_segments,
                              _set_caption_segment)
    cap = "角色甲" + POSE_MARKER + "站立" + COSTUME_MARKER + "红色长裙" + OCCL_BACK
    head, segs, tail = _split_caption_segments(cap)
    assert head == "角色甲"
    assert segs == {"pose": "站立", "costume": "红色长裙"}
    assert tail == OCCL_BACK
    # 拼回顺序固定：姿势 → 服装 → 遮挡后缀
    assert _join_caption_segments(head, segs, tail) == cap
    item = {"caption": cap}
    _set_caption_segment(item, "pose", "跪立")
    assert item["caption"] == "角色甲" + POSE_MARKER + "跪立" + COSTUME_MARKER + "红色长裙" + OCCL_BACK
    _set_caption_segment(item, "costume", "蓝色外套")
    assert item["caption"] == "角色甲" + POSE_MARKER + "跪立" + COSTUME_MARKER + "蓝色外套" + OCCL_BACK
    # 无标记时 head 原样保留
    head2, segs2, tail2 = _split_caption_segments("无标记角色")
    assert (head2, segs2, tail2) == ("无标记角色", {}, "")


def test_apply_costume_single_and_dual():
    from py.translate import COSTUME_MARKER, apply_costume_to_draft
    # 单参考 → 最突出角色（item1，右侧大面积）
    d = apply_costume_to_draft(_mk_pose_draft(), COSTUME_A, "")
    caps = _captions(d)
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert COSTUME_MARKER not in caps[0]
    assert COSTUME_MARKER not in caps[2], "非角色条目不得被注入"
    # 双参考 → 左 costume_in、右 costume_in_2
    d = apply_costume_to_draft(_mk_pose_draft(), COSTUME_A, COSTUME_B)
    caps = _captions(d)
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE), "左角色应得 costume_in"
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_B_PHRASE), "右角色应得 costume_in_2"
    # 重复注入幂等：替换不叠加
    d = apply_costume_to_draft(d, COSTUME_B, COSTUME_A)
    caps = _captions(d)
    assert caps[0].count(COSTUME_MARKER) == 1 and caps[1].count(COSTUME_MARKER) == 1
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_B_PHRASE)
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)


def test_apply_costume_target_and_errors():
    from py.translate import COSTUME_MARKER, apply_costume_to_draft
    # 指定目标下标：只注入选中角色
    d = apply_costume_to_draft(_mk_pose_draft(), COSTUME_A, "", target_index=0)
    caps = _captions(d)
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert COSTUME_MARKER not in caps[1]
    # 只接 costume_in_2 也生效
    d = apply_costume_to_draft(_mk_pose_draft(), "", COSTUME_B, target_index=1)
    assert _captions(d)[1].endswith(COSTUME_MARKER + COSTUME_B_PHRASE)
    # 单角色 + 双参考：只套用 costume_in，不得越界
    solo = {"width": 1024, "height": 1024,
            "bbox_coord_space": "abs", "bbox_format": "xyxy",
            "summary": {"meta": ["特摄"], "canvas": "single", "caption": "场景。"},
            "items": [{"label": "character", "caption": "唯一角色", "bbox": [0, 0, 512, 1024]}]}
    d = apply_costume_to_draft(solo, COSTUME_A, COSTUME_B)
    assert _captions(d)[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert _captions(d)[0].count(COSTUME_MARKER) == 1
    cases = [
        ("关闭档", dict(draft=_mk_pose_draft(), costume_a=COSTUME_A, costume_b="", mode="关闭")),
        ("无服装输入", dict(draft=_mk_pose_draft(), costume_a="", costume_b="")),
        ("只有季节分类", dict(draft=_mk_pose_draft(), costume_a="春装 / ", costume_b="")),
        ("非布局形态", dict(draft={"width": 1024}, costume_a=COSTUME_A, costume_b="")),
        ("无角色条目", dict(draft={"width": 1024, "height": 1024, "items": [
            {"label": "object", "caption": "招牌", "bbox": [0, 0, 10, 10]}]},
            costume_a=COSTUME_A, costume_b="")),
        ("目标越界", dict(draft=_mk_pose_draft(), costume_a=COSTUME_A, costume_b="",
                     target_index=99)),
        ("目标非角色", dict(draft=_mk_pose_draft(), costume_a=COSTUME_A, costume_b="",
                     target_index=2)),
    ]
    for name, kw in cases:
        try:
            apply_costume_to_draft(**kw)
            assert False, f"{name} 应抛 ValueError"
        except ValueError as e:
            assert str(e), name


def test_pose_costume_coexist_and_tail_preserved():
    from py.translate import (COSTUME_MARKER, OCCL_FRONT, POSE_MARKER,
                              _costume_phrase, apply_costume_to_draft,
                              apply_pose_to_draft)
    d = _mk_pose_draft()
    apply_pose_to_draft(d, POSE_A, "")
    apply_costume_to_draft(d, COSTUME_A, "")
    cap = _captions(d)[1]
    assert cap.count(POSE_MARKER) == 1 and cap.count(COSTUME_MARKER) == 1
    assert cap.index(POSE_MARKER) < cap.index(COSTUME_MARKER), "顺序固定为 姿势 → 服装"
    assert cap.endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    # 再注入姿势：服装段必须保留
    apply_pose_to_draft(d, POSE_B, "")
    cap = _captions(d)[1]
    assert cap.count(POSE_MARKER) == 1 and cap.count(COSTUME_MARKER) == 1
    assert POSE_MARKER + "S形站姿・胯部侧顶（正面）" in cap
    assert cap.endswith(COSTUME_MARKER + COSTUME_A_PHRASE), "姿势注入不得删掉服装段"
    # 再注入服装：姿势段必须保留
    apply_costume_to_draft(d, COSTUME_B, "")
    cap = _captions(d)[1]
    assert cap.count(POSE_MARKER) == 1 and cap.count(COSTUME_MARKER) == 1
    assert POSE_MARKER + "S形站姿・胯部侧顶（正面）" in cap, "服装注入不得删掉姿势段"
    assert cap.endswith(COSTUME_MARKER + COSTUME_B_PHRASE)
    # 已知遮挡后缀必须留在最后
    d["items"][1]["caption"] += OCCL_FRONT
    apply_costume_to_draft(d, COSTUME_A, "")
    cap = _captions(d)[1]
    assert cap.endswith(OCCL_FRONT)
    assert cap.index(POSE_MARKER) < cap.index(COSTUME_MARKER)
    assert _costume_phrase(COSTUME_A) in cap


def test_apply_costume_removes_overlapping_old_costume_objects():
    """反推草稿把旧服装拆成 object 时，本地服装注入必须移除它，否则旧衣服会画回去。"""
    from py.translate import apply_costume_to_draft
    d = _mk_pose_draft()
    d["items"] += [
        # 与右侧大角色重叠的旧连衣裙：应移除
        {"label": "object", "caption": "浅粉色无袖连衣裙，领口有三角形装饰",
         "bbox": [600, 200, 1000, 900]},
        # 背景/配饰：不含服装名词，保留
        {"label": "object", "caption": "背景为灰白色斑驳水泥墙面",
         "bbox": [0, 0, 1024, 1024]},
        {"label": "object", "caption": "右手佩戴金色戒指", "bbox": [700, 800, 800, 900]},
    ]
    before = len(d["items"])
    out = apply_costume_to_draft(d, COSTUME_A, "")
    caps = _captions(out)
    assert not any("连衣裙" in c for c in caps), "与角色重叠的旧服装 object 应被移除"
    assert any("水泥墙" in c for c in caps), "背景 object 必须保留"
    assert any("戒指" in c for c in caps), "配饰 object 必须保留"
    assert len(out["items"]) == before - 1
    # 不重叠的场景服装（如橱窗里的裙子）保留
    d2 = _mk_pose_draft()
    d2["items"].append({"label": "object", "caption": "橱窗里的红色连衣裙",
                        "bbox": [0, 0, 100, 100]})
    out2 = apply_costume_to_draft(d2, COSTUME_A, "")
    assert any("橱窗里的红色连衣裙" in c for c in _captions(out2)), "不重叠的场景服装不得误删"
    # 非法 bbox 的重叠比例返回 0（避免清理逻辑自己抛错；草稿仍由 validate_draft 把关）
    from py.translate import _bbox_overlap_ratio
    assert _bbox_overlap_ratio("bad", [0, 0, 10, 10]) == 0
    assert _bbox_overlap_ratio([0, 0, 10, 10], "bad") == 0


def test_apply_costume_never_strips_text_items():
    """label=text 的画面可见文字（促销横幅等）即使命中服装名词且与角色重叠，
    也绝不能被旧服装清理误删——静默删除用户内容（v1.9.0-beta0 审查 P1-1）。"""
    from py.translate import apply_costume_to_draft
    d = _mk_pose_draft()
    d["items"] += [
        # 与右侧大角色重叠、命中服装关键词"鞋"的促销横幅
        {"label": "text", "caption": "鞋类新品大促销横幅", "text": "鞋类新品大促销",
         "bbox": [600, 200, 1000, 500]},
    ]
    before = len(d["items"])
    out = apply_costume_to_draft(d, COSTUME_A, "")
    texts = [it.get("text") for it in out["items"] if it.get("label") == "text"]
    assert "鞋类新品大促销" in texts, "text 条目不得被旧服装清理删除"
    assert len(out["items"]) == before


def test_apply_costume_top_bottom_layout_assigns_upper_first():
    """上下堆叠（x 中心接近）的双人：A 给上面的、B 给下面的——与 LLM 指令
    "A to the upper one" 一致（审查 P2-6：此前本地按 x 排序会取 items 顺序，方向随机）。"""
    from py.translate import apply_costume_to_draft
    d = _mk_pose_draft()
    d["items"] = [
        # 上：x 中心 512；下：x 中心 512（|Δx|=0 < 5% 画布宽）→ 视为上下布局
        {"label": "character", "caption": "上面的人物", "bbox": [200, 100, 824, 500]},
        {"label": "character", "caption": "下面的人物", "bbox": [200, 520, 824, 920]},
    ]
    out = apply_costume_to_draft(d, COSTUME_A, COSTUME_B)
    caps = [(it["caption"], it["label"]) for it in out["items"] if it.get("label") == "character"]
    assert caps[0][0].endswith(COSTUME_A_PHRASE), f"上面的角色应得 A: {caps[0]}"
    assert caps[1][0].endswith(COSTUME_B_PHRASE), f"下面的角色应得 B: {caps[1]}"


def test_editor_costume_inputs_definition():
    opt = MOSSEditor.INPUT_TYPES()["optional"]
    assert opt["costume_in"][1].get("multiline") and opt["costume_in"][1].get("default") == ""
    assert opt["costume_in_2"][1].get("multiline")
    modes, meta = opt["costume_mode"]
    assert list(modes) == ["自动", "关闭"] and meta["default"] == "自动"
    apply_meta = opt["apply_costume"][1]
    assert apply_meta["default"] == -1 and apply_meta["hidden"] and apply_meta["socketless"]
    # 新控件必须全部追加在姿势控件之后，旧工作流 widgets_values 不错位
    names = list(opt)
    assert names.index("costume_mode") > names.index("pose_in_2")
    assert names.index("costume_in") > names.index("apply_costume") > names.index("costume_mode")


def test_editor_is_changed_tracks_costume():
    base = dict(source="散文", canvas_width=1024, canvas_height=1024, edits="",
                ai_draft=True)
    h0 = MOSSEditor.IS_CHANGED(**base)
    assert MOSSEditor.IS_CHANGED(**base, costume_in=COSTUME_A) != h0, "换服装必须让缓存失效"
    assert MOSSEditor.IS_CHANGED(**base, costume_in_2=COSTUME_B) != h0
    assert MOSSEditor.IS_CHANGED(**base, costume_mode="关闭") != h0
    import math
    assert math.isnan(MOSSEditor.IS_CHANGED(**base, apply_costume=0))
    assert math.isnan(MOSSEditor.IS_CHANGED(**base, apply_costume=-2))
    # 旧工作流参数缺省（kwargs 兜底）也不得抛错
    assert MOSSEditor.IS_CHANGED("散文", 1024, 1024, "", True)


def test_prose_to_layout_costume_prompt_assembly():
    """无服装 → system prompt 与历史逐字节一致；有服装 → 末尾追加指令段。"""
    from py.translate import (append_agent_md, costume_directive_block,
                              pose_directive_block)
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 4

        def call(**kw):
            prose_to_layout("一段散文。", width=1024, height=1024,
                            base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
                            api_key="EMPTY", model="m", temperature=0.3,
                            thinking="off", provider="custom", timeout=30, **kw)

        call()
        baseline = append_agent_md(DEFAULT_AI_DRAFT_PROMPT, "")
        assert _last_system_prompt() == baseline, "无服装时 system prompt 不得有任何变化"

        call(costume_a=COSTUME_A, costume_b=COSTUME_B)
        assert _last_system_prompt() == baseline + "\n\n" + \
            costume_directive_block(COSTUME_A, COSTUME_B)

        call(costume_a=COSTUME_A, costume_mode="关闭")
        assert _last_system_prompt() == baseline, "关闭档必须等价于未接入"

        call(pose_a=POSE_A, costume_a=COSTUME_A)
        assert _last_system_prompt() == baseline + "\n\n" + \
            pose_directive_block(POSE_A, "") + "\n\n" + \
            costume_directive_block(COSTUME_A, ""), "姿势段在前、服装段在后"
    finally:
        srv.shutdown()


def test_editor_run_passes_costume_to_llm():
    """Editor 全链路：costume_in/costume_in_2/costume_mode 正确送达拆解 LLM。"""
    srv = _start_mock()
    try:
        MockHandler.queue = [json.dumps(LAYOUT_ZH, ensure_ascii=False)] * 3
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"

        def run_costume(**kw):
            return MOSSEditor().run(
                "一段特摄战斗的纯文本提示词。", 1024, 1024, "",
                ai_draft=True, provider="custom", base_url=url, model="mock",
                api_key="EMPTY", temperature=0.3, thinking="off", timeout=30, ai_prompt="",
                **kw,
            )

        ret = run_costume(costume_in=COSTUME_A, costume_in_2=COSTUME_B, costume_mode="自动")
        assert "mps_draft" in ret["ui"], "服装路径不影响拆解写回"
        assert COSTUME_A in _last_system_prompt() and COSTUME_B in _last_system_prompt()

        run_costume(costume_in=COSTUME_A, costume_mode="关闭")
        assert COSTUME_A not in _last_system_prompt(), "关闭档不得把服装送进 LLM"

        run_costume(pose_in=POSE_A, costume_in=COSTUME_A)
        sys_prompt = _last_system_prompt()
        assert sys_prompt.index("Pose references") < sys_prompt.index("Costume references")
    finally:
        srv.shutdown()


def test_editor_run_apply_costume_writeback_and_errors():
    from py.translate import COSTUME_MARKER, POSE_MARKER
    draft = _mk_pose_draft()
    # ① 自动分配 → ui.mps_draft 写回，输出与写回一致
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False),
                           apply_costume=-2, costume_in=COSTUME_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[1].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert json.loads(ret["result"][0])["items"][1]["caption"] == caps[1]
    # ② 指定目标下标：只注入选中角色
    ret = _run_editor_pose(json.dumps(_mk_pose_draft(), ensure_ascii=False),
                           apply_costume=0, costume_in=COSTUME_A)
    caps = _captions(json.loads(ret["ui"]["mps_draft"]))
    assert caps[0].endswith(COSTUME_MARKER + COSTUME_A_PHRASE)
    assert COSTUME_MARKER not in caps[1]
    # ③ apply_costume 缺省（-1）：普通 Queue 不注入、不产生 ui
    ret = _run_editor_pose(json.dumps(draft, ensure_ascii=False), costume_in=COSTUME_A)
    assert isinstance(ret, tuple) and len(ret) == 3, "无注入时保持普通三元组返回"
    # ④ 姿势 + 服装同一提交：两个分段都写入且顺序正确
    ret = _run_editor_pose(json.dumps(_mk_pose_draft(), ensure_ascii=False),
                           apply_pose=-2, apply_costume=-2,
                           pose_in=POSE_A, costume_in=COSTUME_A)
    cap = _captions(json.loads(ret["ui"]["mps_draft"]))[1]
    assert POSE_MARKER in cap and COSTUME_MARKER in cap
    assert cap.index(POSE_MARKER) < cap.index(COSTUME_MARKER)
    # ⑤ 输入是散文：注入须明确报错
    try:
        _run_editor_pose(json.dumps(draft, ensure_ascii=False), source="一段散文。",
                         sig=source_signature("一段散文。") + "x",
                         apply_costume=-2, costume_in=COSTUME_A)
        assert False, "caption 形态注入应报错引导用户先拆解"
    except ValueError as e:
        assert "拆解" in str(e)
    # ⑥ costume_mode=关闭：拒绝注入
    try:
        _run_editor_pose(json.dumps(draft, ensure_ascii=False),
                         apply_costume=-2, costume_in=COSTUME_A, costume_mode="关闭")
        assert False, "关闭档应拒绝注入"
    except ValueError as e:
        assert "costume_mode" in str(e)
    # ⑦ IS_CHANGED：apply_costume != -1 绕过缓存
    import math
    assert math.isnan(MOSSEditor.IS_CHANGED("散文", 8, 8, "", False, apply_costume=-2))
