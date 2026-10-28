# MOSS Prompt Studio（ComfyUI 插件）

把上游 agent 产出的**中文 MOSS-Image 提示词草稿**，在 ComfyUI 里可视化编辑、翻译成英文、按官方口径编译，直接接 ComfyUI 原生 MOSS-Image 节点生图。

```
[其他设备] agent + MOSS官方skill ──产──> 中文草稿 JSON (caption 或 layout)
        │ 手动复制粘贴到 Editor 节点
        ▼
MOSSEditor ──draft_zh──> MOSSTranslator ──draft_en──> MOSSCompiler
   (画布中文编辑)          (MiMo/DeepSeek 中→英)        (官方编译器移植)
                              ▲ Ctrl+B 可旁路             │prompt/negative/width/height
                                                         ▼
                                        ComfyUI 原生 MOSS-Image 节点
                                                         │
                                          生成图可回接 Editor 当底图对比迭代
```

> caption 形态没有框，草稿 JSON 里不含尺寸：把 Editor 的 `width`/`height` 输出接到 Compiler 的 `canvas_width`/`canvas_height` 输入，生图尺寸才会与画布一致（示例工作流已接好）。layout 形态的尺寸随草稿自动传递，无需额外连线。

## 安装

把本目录放进 `ComfyUI/custom_nodes/`（zip 解压或 git 均可），重启 ComfyUI。后端零第三方依赖（纯标准库）；底图功能依赖 Pillow（ComfyUI 自带）。

```
custom_nodes/comfyui-moss-prompt-studio/
├── __init__.py            # 节点注册 + WEB_DIRECTORY
├── nodes/                 # 三个节点（编辑器 / 翻译器 / 编译器）
├── py/                    # 编译器移植 / 三形态识别 / 翻译核心（可独立测试）
├── web/js/editor_canvas.js# 画布前端扩展
├── agents/                # 可下拉选择的追加注入指令文件（附示例 cinematic-example.md）
├── AGENT.example.md       # 根目录默认注入文件的模板
├── config.example.json    # LLM 连接配置模板（复制为 config.json 使用）
├── examples/              # 示例工作流 + 中文示例草稿
├── tests/run_tests.py     # 离线测试（22 项）
└── LICENSE                # MIT
```

## 快速使用

1. **配置模型**：复制 `config.example.json` 为 `config.json`，填好 `mimo` / `deepseek` / `custom` 的 `base_url`、`model`、`api_key`；也可以直接在 Editor / Translator 节点的 `provider`、`base_url`、`model`、`api_key` 里填（节点值优先）。
2. **贴入提示词**：把上游 agent 产出的**中文散文提示词**粘贴到 **MOSS 提示词编辑器 (Editor)** 的 `source` 框。
3. **打开拆解**：把 Editor 的 `ai_draft` 改为 `true`。
4. **执行拆解**：点 Editor 工具栏的 **⚡拆解**，等待生成带 bbox 的布局画布；之后可以直接拖动、改元素说明、调配色。
5. **跑完整流程**：`Editor.draft → Translator.draft → Compiler.draft`，再把 `Compiler.prompt / width / height` 接到 MOSS-Image 采样节点；caption 形态建议把 Editor 的 `width`/`height` 接到 Compiler 的 `canvas_width`/`canvas_height`（示例工作流已接好）。改完元素说明后点 Queue 即可，最新的 `edits` 会自动写回。

> 如果上游已经给出 layout 草稿 JSON，直接贴进 `source` 即可，不需要打开 `ai_draft`；纯文本散文才需要拆解。

## 节点说明

### 1. MOSS 提示词编辑器（MOSSEditor）

- **source**（粘贴框，只读语义）：贴上游产出的中文草稿。三种形态**自动识别**：
  `{"caption":...}`（caption 形态）/ layout 草稿（`abs`+`xyxy`，官方 skill 产出形态）/ 已编译单行 JSON（`abs_centralized`+`yxyx`，会按 canvas_width/height 反解回草稿再编辑）；**纯文本散文**也能贴（自动按 caption 处理，配合 AI 拆解直接生成布局）。
- **画布**（节点内 DOM widget）：
  - 拖拽移动、四角缩放、**选中框滚轮缩放**（以中心为锚点等比缩放，Ctrl/Shift 精细档）、方向键微调（Shift=10px）、Delete 删除、Ctrl+Z/Y 撤销重做（100 步）；勾选"16吸附"时所有交互对齐 16 像素网格；
  - 图层面板（↑↓ 调前后关系）、检查器（改 caption / text / tags / palette / summary / label）；
  - **palette 主配色模块**：hex 色块编辑器（点色块开原生拾色器、× 删除、＋ 添加），全局 `summary.palette` 与每个元素的 `items[].palette` 两层都支持；字段不存在时可直接从零添加；
  - **meta 体裁模块**：`summary.meta` 体裁标签（逗号分隔字符串数组），字段不存在时可直接添加；元素级对应 `tags` 标签同样常驻可编辑；
  - palette / meta / tags 都是独立 JSON 字段进入提示词（不拼进 caption）；翻译时 palette 里的 hex 颜色逐字保留，描述色名正常翻译；
  - **重叠检测**：框重叠时给出一键"谁在前"按钮，自动往 caption 里追加官方要求的遮挡措辞（`，处于最前层，部分遮挡相邻元素` / `，被前方元素部分遮挡`）；
  - 比例 × 像素规模预设（像素档 0.25M/0.5M/1M/1.5M/2M/3M/4M，对应官方支持范围 0.25M–4M、推荐 1M/1.5M/2M；比例含 1:1、5:4、4:3、3:2、16:10、16:9、2:1、21:9、3:1 及对应竖版，官方允许任意比例，只要求宽高为 16 的倍数）；只选比例保持当前像素面积，只选像素规模保持当前比例，宽高自动取 16 倍数并等比缩放全部框；caption 形态没有框可缩放，直接改写 `canvas_width`/`canvas_height` 作为 AI 拆解目标；比例下拉里的“自定义宽高…”可直接输入任意 16 倍数尺寸；下拉框实时回显当前生效的比例/像素值；
  - 实时校验状态条（与编译器同一套规则）；
  - **底图**：`underlay` 输入口回接生成图，执行后自动垫在画布下做对比；
  - "⧉复制"一键复制编辑后的草稿 JSON；"⟲重置"回到粘贴源；
  - **LLM 配置预设**（💾 保存 / 🗑 删除 / 下拉切换）：把 provider/base_url/model/api_key/temperature/thinking/timeout 存成命名预设，编辑器与翻译器共享同一个预设库（存在 ComfyUI userdata，`moss_studio_llm_presets.json`）。
- **AI 拆解**（`ai_draft` 勾选，或直接点工具栏 **⚡拆解**）：source 是散文/caption 时调用 LLM（与翻译节点同一套连接配置）生成官方 layout 草稿（中文条目、1–6 个元素、自动处理遮挡措辞），结果写回 edits 并刷新画布；`ai_prompt` widget 可整体改写拆解指令。拆解进行中画布保持现状、按钮显示进度，完成自动刷新；失败/超时有明确提示。⚡拆解 只提交编辑器单节点，不跑整个工作流。
- **edits**（自动写回）：画布每次编辑自动写进这个 widget，**非空时优先进 source**；检查器里的文字修改在失焦、点击 Queue 或按 Ctrl/Cmd+Enter 前会立即 flush，确保下游翻译/编译节点拿到最新说明；编辑状态随工作流保存/重载/API 无头运行不丢。caption 形态退化为文本编辑。
- 输出：`draft`（编辑后草稿 JSON，STRING）、`width`/`height`（当前画布尺寸，INT；layout 取草稿尺寸，caption 取 `canvas_width`/`canvas_height` 并对齐 16 倍数）。把这两个输出接到 Compiler 的 `canvas_width`/`canvas_height` 输入口，caption 形态的生图尺寸也会与画布一致，且 Compiler 的 width/height 显示会自动跟随。

### 2. MOSS 提示词翻译器（MOSSTranslator）

中文草稿 → 英文草稿。**provider 纯手动切换（无自动 failover）**。

| Widget | 说明 |
|---|---|
| provider | `mimo` / `deepseek` / `custom` |
| base_url | OpenAI 兼容地址；留空读 `config.json`；deepseek 预设 `https://api.deepseek.com/v1`（线上 API） |
| model | 留空读 config / 预设 |
| api_key | `EMPTY`/留空读 config；内网免鉴权可保持 EMPTY |
| thinking | `off/low/medium/high`，默认 off。**off 会显式关闭思考**：deepseek 发 `reasoning_effort:"none"`，mimo 发 `thinking:{"type":"disabled"}`。deepseek 档位 low→low、medium→high、high→max；mimo 是二值开关，low/medium/high 都等于开启（v2.6 flash 默认开启，不显式关闭会慢数倍）；custom 非 off 发 `reasoning_effort`，不认就用 extra_body 覆盖 |
| LLM 配置栏 | 节点顶部与编辑器共享的预设栏（下拉套用 / 💾 保存 / 🗑 删除），多套连接配置一键切换 |
| extra_body | 原始 JSON 合并进请求体（**最后合并、可覆盖一切**），如 `{"chat_template_kwargs":{"thinking":true}}` |
| system_prompt | 默认内建"翻译契约"，可整体改写；清空即恢复默认 |

**三个提示词注入口**：① `system_prompt` widget（静态改写契约）；② `extra_instructions` 输入口（连线动态注入逐次指令，追加到对话末）；③ `extra_body`（注入请求体参数）。

## 提示词注入：完全接管 LLM 行为

五个注入口覆盖文件级持久指令、系统提示词、逐次指令、请求体参数、拆解指令，可以任意组合。**`provider=custom` 指向自部署模型（vLLM / Ollama / sglang 等）时，翻译与拆解的全部行为都由你的提示词接管**——自部署场景下模型策略由你自己定义，这几层注入就是完整的控制面。

| 注入口 | 位置 | 层面 | 典型用法 |
|---|---|---|---|
| ⓪ `AGENT.md` / `agent_md` 下拉 | 插件根目录文件或 `agents/*.md`（节点内像 LoadImage 一样下拉选择） | **追加注入**：全文以托管块标记包裹，追加在系统提示词末尾（不替换任何内容），翻译与 AI 拆解同时生效；每次执行自动重读，改文件即刻生效 | 持久化术语表/风格规则，多套文件一键切换 |
| ① `system_prompt` | 翻译器 widget | 系统提示词（整体替换默认翻译契约，清空恢复默认；AGENT.md 仍追加其后） | 固定术语表、风格契约、输出格式加固 |
| ② `extra_instructions` | 翻译器输入口 | 追加到当次对话末尾的逐次指令（可连线动态切换） | 按批次注入不同风格要求 / 词汇替换规则 |
| ③ `extra_body` | 翻译器 widget | 原始 JSON 最后合并进请求体，**可覆盖一切参数** | `{"temperature":0.1}`、`{"chat_template_kwargs":{"thinking":true}}`、关闭/改写 reasoning 参数 |
| ④ `ai_prompt` | 编辑器 widget | 整体替换 AI 拆解的默认指令（清空恢复默认；AGENT.md 仍追加其后） | 改元素数量上限、中文条目长度、构图规则 |

### AGENT.md 追加注入（Codex-X 风格，节点内可选文件）

机制移植自 [Codex-X](https://github.com/yynxxxxx/Codex-X) 的 Append 模式，用法两种：

- **默认**：把 `AGENT.example.md` 复制为 `AGENT.md` 放插件根目录（与 config.json 同级），节点 `agent_md` 下拉保持"(默认 AGENT.md)"；
- **多套切换**：把 .md 文件放进插件 `agents/` 目录（自带示例 `agents/cinematic-example.md`），在编辑器/翻译器节点的 `agent_md` 下拉框里选择——像 ComfyUI 载入图片一样列文件选文件，新文件刷新页面后出现在列表里；选"(不注入)"完全关闭。

行为细节：

- **追加而非替换**：内容包在 `<!-- MOSS-STUDIO:AGENT:BEGIN/END -->` 托管块标记里，追加在系统提示词（默认契约或 widget 改写后的契约）末尾；文件里标记之外的内容不注入，可用于记笔记；
- 文件不存在/为空 = 完全不影响原行为；标记不成对、文件不存在会明确报错；
- **自动替换**：每次执行都重新读盘——改文件内容、换下拉选项，下一次执行即刻生效，无需重启 ComfyUI。

示例——`system_prompt`（整体替换契约）+ `AGENT.md`（追加）+ `extra_instructions`（逐次）三层叠加的最终系统提示词形态：

```
（system_prompt widget 内容，或默认契约）
<!-- MOSS-STUDIO:AGENT:BEGIN -->
（AGENT.md 全文）
<!-- MOSS-STUDIO:AGENT:END -->
```

示例——`extra_instructions` 连一个字符串节点按批次切换：

```
This batch: keep all product names in original Chinese pinyin.
```

> 提示：对商用 API（DeepSeek/MiMo 等）使用注入口时请遵守对应服务的使用条款；注入口的设计目标是自部署模型控制与翻译质量调优。

**结构完整性校验**：输出必须 schema 一致、bbox 逐字不变、`text`（画面可见文字）逐字保留、label/枚举/宽高不动，只译 `caption/meta/tags/palette` 的描述性文字。违规/HTTP 失败 → 带错误自动重试 ≤2 次 → 仍失败红字报错，绝不输出半成品。思考模型的 `reasoning_content` 不会混入草稿。LLM 输出的常见 JSON 手误（字符串内未转义引号、结尾缺括号）会先自动修复再进校验。

### 3. MOSS 提示词编译器（MOSSCompiler）

移植官方 `compile_prompt.py`：严格校验（未知字段/越界框/非 16 倍数/重复键/NaN 全部拒绝）+ 坐标编译（abs/xyxy → abs_centralized/yxyx，边缘 floor/ceil，去 width/height，单行紧凑输出）。

**宽高来源**（三种输入形态互不混淆）：

| 输入形态 | 尺寸来源 |
|---|---|
| layout 草稿（带 `width`/`height` 和 `items[].bbox`） | 始终用草稿里的 `width`/`height`；连线/widget 都不覆盖 |
| caption（`{"caption": ...}`） | `canvas_width`/`canvas_height` 连线优先；未连线才用本节点 `width`/`height` widget（默认 1024） |
| 已编译（`abs_centralized` + `yxyx`） | 同上：连线优先，未连线用 widget |

`canvas_width`/`canvas_height` 接 Editor 的 `width`/`height` 输出（必须为 16 的正整数倍）。把 Compiler 的 `width`/`height` 输出接到 MOSS-Image 采样节点，即可保证生成尺寸与画布一致；layout 形态即使接了连线也不会覆盖草稿尺寸。

**界面联动**：一旦 `canvas_width`/`canvas_height` 连线，Compiler 的 `width`/`height` widget 会由前端实时跟随 Editor 的当前画布尺寸（约 0.5 秒内），不需要手动改；调整 Editor 的比例、像素档或自定义宽高后，Compiler 里的数字会同步变化。这只影响界面显示，执行时仍按上面的优先级取连线值。

**自动补线**：旧工作流保存的 Compiler 可能没有 `canvas_width`/`canvas_height` 这两个输入或连线，刷新后会丢。现在 Compiler 加载时会沿 `draft` 上游追溯（`Editor → Translator → Compiler`，或 Editor 直连 Compiler），自动把该 Editor 的 `width`/`height` 输出接到自己的 `canvas_width`/`canvas_height`；已经手动接好的不覆盖。刷新后 Compiler 会自动恢复联动，保存工作流后连线也会一并保存。

输出：`prompt`（接 MOSS-Image 文本编码节点正向提示词）、`negative`（默认官方推荐值，可改）、`width`/`height`（layout 取草稿值；caption/已编译优先取 `canvas_width`/`canvas_height` 连线，未连线才用节点 widget）、`clip_text`（给 CLIP 文本编码器的单口文本，默认=prompt，勾选 `clip_include_negative` 时在其后拼接负向提示词）。

## 上游契约（给内网 agent 的要求）

产出**中文**草稿 JSON（官方 skill 的 layout 草稿形态：`abs`+`xyxy`+16 倍数宽高，或 caption 形态），`text` 字段（画面可见文字）中文原样保留——直接整段粘贴即可，无需英文版、无需备注文件。参考 `examples/example_draft_zh.json`。上游给一段中文散文也行：贴进 source 后用 AI 拆解（⚡拆解）自动生成布局草稿。

## 默认配置：MiMo Token Plan 与 DeepSeek API

开箱即用的两条线路，二选一或同时配置（`config.json` + provider 下拉切换，也可以直接在节点 widget 里填）：

### MiMo Token Plan（小米 MiMo 套餐）

```json
{
  "mimo": {
    "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
    "model": "mimo-v2.6-flash",
    "api_key": "tp-你的MiMo套餐密钥"
  }
}
```

- 套餐地址与模型名以套餐文档为准（形如 `https://xxx/v1`、模型名如 `mimo-v2.6-flash`）
- api_key 用套餐发放的 `tp-` 开头密钥
- 内网自部署 MiMo 则把 base_url 换成内网地址即可（免鉴权时 api_key 保持 `EMPTY`）
- **`mimo-v2.6-flash` 默认开启深度思考**：插件的 `thinking=off` 会显式发 `thinking:{"type":"disabled"}` 关闭；开启思考时 temperature/top_p 会被服务端忽略。插件同时发 `max_completion_tokens` 限制“思考+正文”总长度

### DeepSeek API

```json
{
  "deepseek": {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-flash",
    "api_key": "sk-你的DeepSeek密钥"
  }
}
```

- base_url 留空时 deepseek provider 自动使用官方地址 `https://api.deepseek.com/v1`
- 模型名留空时默认 `deepseek-chat`（官方别名）；也可显式指定 `deepseek-flash` / `deepseek-v4-pro`
- 思考强度经 `reasoning_effort` 参数控制（off → `none` 显式关闭；low/medium/high → low/high/max），无需更换模型名

### 通用说明

复制 `config.example.json` 为 `config.json` 放本目录，按 provider 存 base_url/model/api_key；**widget 值优先于 config**；节点顶部的「LLM 配置预设」（💾/🗑）可以把多套线路存成命名预设一键切换。避免把 API key 序列化进工作流文件（工作流里建议保持 `EMPTY`，key 放 config.json 或预设库）。

## 测试

```
python tests/run_tests.py
```

覆盖官方口径编译（含官方文档给出的期望坐标）、三形态识别与反解往返、节点行为、翻译结构校验、endpoint/请求体组装、mock HTTP 下的成功/重试/失败路径。

## 排查 LLM 耗时

每次模型调用都会在 ComfyUI 控制台输出一行 `[MOSS Studio] LLM ...`：

```
[MOSS Studio] LLM draft attempt=1/3 2.14s host=api.deepseek.com model=deepseek-flash thinking=None effort=none finish=stop completion=243 reasoning=-
```

- `attempt=1/3`：第几次调用；同一批出现多行表示发生了重试。
- `thinking` / `effort`：实际发出的思考开关。MiMo 应为 `{'type': 'disabled'}`，DeepSeek 应为 `effort=none`；如果这里是 `None`，说明节点没走到对应的 provider/thinking 配置。
- `completion`：总输出 token；`reasoning`：其中思考 token（`-` 表示响应没有该字段）。`thinking=off` 时 `reasoning` 应为 0 或 `-`；如果是几千，说明服务端没有遵守关闭参数。
- `[MOSS Studio] AI 拆解已本地修复: ...`：越界框/多余字段被自动修正，没有触发重试。
- `[MOSS Studio] AI 拆解 attempt=N 失败: ...`：该次输出仍不合规，会带错误重试。
- `finish=length` 表示输出被 token 上限截断。

## 设计依据与致谢

- 编译器与校验规则移植自 [OpenMOSS/MOSS-Image](https://github.com/OpenMOSS/MOSS-Image) 官方 skill（`skills/moss-image-prompt-enhancer`），保证与官方口径一致。
- 画布交互改编自 [swan7-py/json-prompt-visualizer](https://github.com/swan7-py/json-prompt-visualizer) 的 canvas.py（拖拽/缩放/撤销/重叠闸门/坐标换算思路），适配为 ComfyUI 前端扩展。
- 指令文件的追加注入语义参考了 [Codex-X](https://github.com/yynxxxxx/Codex-X) 的 Append 托管块设计。

## 许可

MIT（见 [LICENSE](LICENSE)）。

## 已知限制

- 画布前端在 ComfyUI API 无头模式下不渲染（edits widget 值照常生效）。
- DeepSeek 思考强度细档位（low/medium/high 的差异）取决于其 API 当时的参数支持，必要时用 extra_body 指定。
- 粘贴已编译 JSON 时若 canvas_width/height 与原图不符，反解出的框位置会偏移——按原图尺寸设置后再粘。
