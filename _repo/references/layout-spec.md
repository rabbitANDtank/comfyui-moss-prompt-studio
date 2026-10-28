# MOSS-Image layout 规范（中文速查）

来源：官方 `skills/moss-image-prompt-enhancer/references/layout.md` + `scripts/compile_prompt.py`。
官方原话：**新稿统一用 `summary` + 扁平 `items`**；编译器会把 `elements` 判为未知字段直接报错。

## 一、两个对象各管什么

| 对象 | 回答什么问题 | 一句话 |
|---|---|---|
| `summary` | 整张图是什么 | 给外行看的总述，不是标题、不是广告语 |
| `items[]` | 图里每一块长什么样、在哪 | 逐个点名的清单，每项带一个位置框 |

## 二、summary 字段

| 字段 | 必填 | 含义 | 取值 |
|---|---|---|---|
| `caption` | ✅ | 整张图的连贯描述：构图、主体、场景、材质、光、风格 | 散文，建议 60–120 词 |
| `meta` | | 体裁标签，告诉模型"这是什么东西" | 字符串数组，如 `["poster","studio photography"]` |
| `canvas` | | 画布结构 | `single` 单幅 / `multi_panel` 多格 / `collage` 拼贴 |
| `palette` | | 全局配色倾向 | 十六进制数组 |

## 三、items 字段

| 字段 | 必填 | 含义 | 取值 |
|---|---|---|---|
| `label` | ✅ | 这块属于哪一类 | `object` 物体 / `character` 人物 / `text` 文字 |
| `caption` | ✅ | 这块**看起来**什么样：外形、材质、颜色、字体、描边 | 散文 |
| `bbox` | ✅ | 位置与大小，四个数 | 见下方坐标 |
| `text` | text 类必填 | 要渲染的**逐字内容**；换行写 `\n` | 原文照抄 |
| `tags` | | 补充标签 | 字符串数组 |
| `palette` | | 这一块自己的配色 | 十六进制数组 |

官方明确不接受：`children` / `parent` / `id` / `elements`（旧 scene 格式）/ 任何展示性备注。

## 四、坐标两套口径

| 阶段 | 字段 | 顺序 | 原点 | 说明 |
|---|---|---|---|---|
| 草稿（人写） | `bbox_coord_space:"abs"` `bbox_format:"xyxy"` | `[x1,y1,x2,y2]` | 左上 | 必须同时给 `width`/`height` |
| 编译后（模型看） | `bbox_coord_space:"abs_centralized"` `bbox_format:"yxyx"` | `[y1,x1,y2,x2]` | 画布中心 | 不含 width/height |

换算由官方编译器做：先 floor 左上、ceil 右下，再各减 `W/2`、`H/2`。**不要手算**——画布的导出页签和 `compile_moss.py` 都按这套算，且结果逐字一致。
画布宽高必须是 **16 的正整数倍**；bbox 必须完全在画布内，越界会被拒绝。

## 五、前后关系与遮挡

官方规范**没有 z 字段**，只说了一句：*"Several items may share or overlap a box when the intended image does."*

所以遮挡只能靠 `caption` 写清楚：

```json
{ "label": "object",
  "caption": "A bone-white ceramic cup standing in front of the glass carafe; its left side overlaps
              and occludes the carafe's lower-left body, with a small contact shadow under the base.",
  "bbox": [420, 900, 860, 1290] }
```

要点：**谁在前、挡住了谁的哪一部分、阴影落在哪**，三件事写在同一句里。
数组顺序在编辑器里当图层用，但官方没声明它是机制，别把它当保证。

## 六、三个最容易写错的地方

1. 把 `summary.caption` 写成标题或广告语 —— 它要的是整张图的描述。
2. 以为 bbox 是"让它画个框"。官方明说：**框是位置标注，不是边框或贴纸**。
3. 把要渲染的字塞进 `caption`。字必须进 `text` 字段，否则只是被描述、不被逐字渲染。

## 七、经验

- **换行必须手写**：自动换行不可靠，`text` 里显式写 `\n`。
- **背景用一个全画布 item** 承载（纸面/场景），别只写在 caption 里。官方每个 layout 的第一个元素都是这种。
- 文字块越碎越难渲染：同字数下 18 块明显差于 1 块（实测）。
- 画布尺寸别随手改：bbox 是绝对像素，改尺寸不会等比缩放。
