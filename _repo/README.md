# JSON Prompt Visualizer

把「JSON + bbox」的布局提示词，变成**能看、能改、能导出**的画布。

单文件 HTML，双击就能用：拖动改位置、调前后层级、增删元素、撤销重做，最后一键导出提示词。
不联网、不调模型接口、不需要起服务器。

```
JSON 布局提示词 ──► 画布（框 = 元素，框上直接写着这块是什么）
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
  MOSS 提示词        草稿 JSON        Klein 编辑稿
（单行，贴 ComfyUI）（继续改／存档）（框内改字换物）
```

## 为什么做这个

写 layout 提示词时，最难受的不是写文案，是**看不见坐标**：

- `bbox: [420,900,860,1290]` 到底压在哪？和隔壁那块打不打架？
- 改了 `caption` 之后，画面里那块东西会不会跑？
- 前后遮挡关系，到底该写进哪个字段？

它把这些问题变成一眼能看的事：框画在原图上，框里直接写着「这块是什么、要出什么字」，
中文英文并排，改哪边都行。

## 快速开始

```bash
# 1. 生成本地画布（会写一个 html，双击打开）
python scripts/canvas.py examples/coffee_overlap.draft.json \
        --notes examples/coffee_overlap.notes.json \
        -o examples/coffee_overlap.html

# 2. 已经有出图？叠上去对框
python scripts/canvas.py examples/coffee_overlap.draft.json --image out.png   # 底图内嵌进 HTML
```

打开 HTML：

| 操作 | 效果 |
|---|---|
| 拖框体 / 拖四角 | 移动 / 缩放 |
| 双击框 | 跳到对应字段 |
| `Del` | 删除选中 |
| 方向键（Shift ×10） | 微移 |
| `Ctrl+Z` / `Ctrl+Shift+Z` | 撤销 / 重做 |
| `Ctrl+S` | 打开导出 |
| ↑ ↓ | 调数组顺序（＝图层前后） |
| 顶栏「比例 / MP」 | 选 21:9 之类的比例与百万像素，自动算分辨率并等比外扩画布 |
| 右侧「重叠与前后」 | 框重叠时列清单，一键复制询问单交给 skill 定前后 |

## 导出的东西

- **MOSS 提示词**：单行 JSON，`abs_centralized` + `yxyx` 中心原点，直接贴 `CLIPTextEncode`。
  浏览器里的编译结果与官方 `compile_prompt.py` **逐字一致**（已用样例比对：2446 字符，解析后完全相等）。
- **草稿 JSON**：`abs` + `xyxy` + `width/height`，能被 `canvas.py` 原样读回（round-trip 已验证）。
- **Klein 编辑稿**：指令段 + 一张 `{id, from, src_bbox, tgt_bbox, desc}` 表，`0–1000` 左上原点 `[y,x,y,x]`。
- **中文参考**：`notes.json`，下次 `--notes` 传回来，中文不丢。

导出前会校验：16 倍数、越界、文字块缺 `text`、caption 为空；并提示「框太矮字会很小」「单块字太多」
「没有全画布背景」这类坑。**有错就不编译**，不静默放过。

## 文件结构

```
canvas.py            生成画布（编辑 + 导出）
compile_moss.py      官方编译器（MOSS 草稿 → 单行提示词）
to_klein.py          layout → Klein i2i_bbox
sync.py              同步到 WorkBuddy 用户级 skills 目录并校验
```

## 它不做什么

- 不出图，也不调用任何模型；只处理 JSON、坐标和预览。
- 中文参考不导出（官方编译器不认），别把唯一文案只写在中文字段。
- 换画布尺寸不会等比缩放 bbox——坐标是绝对像素。
- **JSON 没有 z 字段**：前后关系靠 caption 文字表达，数组顺序只是编辑器内的图层语义。

## 适配其他模型

`canvas.py` 只管「读 JSON → 画 → 改 → 导出」，格式差异集中在两处：
`buildState()`（读入时把 bbox 统一成左上原点像素）和导出函数（`compiledObj` / `kleinRows`）。
接新模型就是加一个「导入适配 + 导出适配」，画布与交互不用动。

## License

MIT
