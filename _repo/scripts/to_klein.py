# -*- coding: utf-8 -*-
"""把 MOSS-Image 的 layout JSON 转成 Klein 的 i2i_bbox 编辑稿。

为什么要这一步：MOSS-Image 只能文生图，不支持图像编辑；Klein 支持"框内改"。
两者对 bbox 的口径不同，这里做换算 + 生成 Klein 训练格式的标注表。

坐标换算
  MOSS 草稿  [x1,y1,x2,y2] 像素 / 左上原点  →  /W*1000, /H*1000
  MOSS 编译稿 [y1,x1,y2,x2] 像素 / 中心原点  →  先 +W/2、+H/2，再 /W*1000, /H*1000
  Klein 要求 [y_min,x_min,y_max,x_max] 整数 0–1000 / 左上原点 / 紧贴对象 / 文字四周留 5–15 单位

用法
  # 新建（从零生成一张）
  python to_klein.py layout.json --instruction "Create a warm studio poster of a ceramic cup." --out klein.txt

  # 编辑已有图（所有行来自参考图 0）
  python to_klein.py layout.json --from-image 0 \
      --instruction "Change the title to 夜间手冲 on the same poster." --out klein.txt

  # 只想看表
  python to_klein.py layout.json --instruction "..." --pretty
"""
import argparse
import json
import os
import re
import sys

CJK = re.compile(r"[\u4e00-\u9fff]")


def load(path):
    lay = json.load(open(path, encoding="utf-8"))
    return json.loads(lay) if isinstance(lay, str) else lay


def to_px(lay, W, H):
    """统一成左上原点像素 [x1,y1,x2,y2]。"""
    space = lay.get("bbox_coord_space", "abs")
    out = []
    for it in (lay.get("items") or lay.get("elements") or []):
        b = it.get("bbox")
        if not b or len(b) != 4:
            continue
        if space == "abs_centralized":
            y1, x1, y2, x2 = b
            box = [x1 + W / 2, y1 + H / 2, x2 + W / 2, y2 + H / 2]
        else:
            box = list(b)
        out.append({
            "label": it.get("label") or it.get("type") or "object",
            "text": it.get("text") or "",
            "caption": it.get("caption") or it.get("identity") or "",
            "box": box,
        })
    return out


def to_klein_box(box, W, H, margin=0.0):
    x1, y1, x2, y2 = box
    x1, x2 = x1 / W * 1000 - margin, x2 / W * 1000 + margin
    y1, y2 = y1 / H * 1000 - margin, y2 / H * 1000 + margin
    cl = lambda v: int(max(0, min(1000, round(v))))
    return [cl(y1), cl(x1), cl(y2), cl(x2)]


def make_id(item, idx, counters, text_prefix):
    if item["label"] in ("text",):
        counters["text"] += 1
        py = "Cn" if CJK.search(item.get("text", "")) else "En"
        return "%s_Text_%d" % (py if text_prefix == "auto" else text_prefix, counters["text"])
    # 物体：从 caption 里取第一个"像类名词"的词，跳过冠词与形容词
    SKIP = {"a", "an", "the", "flat", "solid", "single", "one", "two", "three", "large",
            "small", "tiny", "big", "bold", "simple", "clean", "warm", "dark", "light",
            "high", "low", "full", "matte", "off", "white", "black", "red", "blue", "green"}
    cap = (item.get("caption") or "").strip()
    word = ""
    for tok in re.findall(r"[a-zA-Z][a-zA-Z\-]*", cap)[:8]:
        t = tok.lower()
        if t not in SKIP and len(t) > 2:
            word = t
            break
    word = re.sub(r"[^a-z\-]", "", word) or "object"
    counters[word] = counters.get(word, 0) + 1
    return "%s_%d" % (word, counters[word])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("layout")
    ap.add_argument("--instruction", required=True, help="英文指令段（编辑用 Change/Remove，新建用 Create a ... of ...）")
    ap.add_argument("--from-image", type=int, default=None, help="编辑模式：所有行来自 ref_image_N")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--text-margin", type=float, default=8.0, help="文字框四周留白（Klein 要求 5–15）")
    ap.add_argument("--text-prefix", default="auto", choices=["auto", "En", "Cn"])
    ap.add_argument("--pretty", action="store_true", help="只打表，便于阅读")
    ap.add_argument("--out", help="把单行记录写到文件")
    a = ap.parse_args()

    lay = load(a.layout)
    W = lay.get("width") or a.width
    H = lay.get("height") or a.height
    if not W or not H:
        sys.exit("缺画布尺寸：草稿要有 width/height，编译稿请传 --width/--height")

    items = to_px(lay, W, H)
    counters = {"text": 0}
    rows = []
    for it in items:
        margin = a.text_margin if it["label"] == "text" else 0.0
        box = to_klein_box(it["box"], W, H, margin)
        row = {
            "id": make_id(it, len(rows), counters, a.text_prefix),
            "from": "target" if a.from_image is None else "ref_image_%d" % a.from_image,
            "src_bbox": None if a.from_image is None else box,
            "tgt_bbox": box,
            "desc": it["caption"] or ("text: " + it["text"]),
        }
        if it["label"] == "text" and it["text"]:
            row["desc"] = (row["desc"] + " Text reads: \"%s\"." % it["text"]).strip()
        rows.append(row)

    para = a.instruction.strip()
    if a.from_image is not None and not para.lower().rstrip(".").endswith("stays identical to the source"):
        para = para.rstrip(".") + ". Everything else stays identical to the source."

    record = "%s %s" % (para, json.dumps(rows, ensure_ascii=False, separators=(",", ":")))

    if a.pretty:
        print("指令段：\n  " + para + "\n")
        print("%-14s %-12s %-22s %-22s %s" % ("id", "from", "src_bbox", "tgt_bbox", "desc"))
        for r in rows:
            print("%-14s %-12s %-22s %-22s %s" % (
                r["id"], r["from"], str(r["src_bbox"]), str(r["tgt_bbox"]), r["desc"][:60]))
        print("\n共 %d 行；文字行四周留白 %.0f 单位" % (len(rows), a.text_margin))
        return

    if a.out:
        open(a.out, "w", encoding="utf-8").write(record + "\n")
        print("写出 %s（%d 行，单行记录）" % (a.out, len(rows)), file=sys.stderr)
    else:
        print(record)

    print("\n提醒：Klein 要求指令段第一次提到每个对象时带 <id> 引用，且每个 id 恰好出现一次。"
          "\n      这里是几何与表格的换算结果，引用位置需在指令段里人工落到具体句子。",
          file=sys.stderr)


if __name__ == "__main__":
    main()
