# MOSS Prompt Studio（ComfyUI 插件）

当前版本：**v1.8.0**

> **本次更新**：新增服装注入与姿势服装对调，修连续注入误判、旧衣残留，拆解键高亮（详见「版本与更新」）。旧工作流中的 Editor 节点请**删除后重新添加，并按新节点重新连线**。

把上游 agent 产出的**中文 MOSS-Image 提示词草稿或散文式提示词**，在 ComfyUI 里可视化编辑、翻译成英文、按官方口径编译，直接接原生 MOSS-Image 节点生图。

```
LoadImage ──> ImageReverse（可选）──> Editor ──> Translator ──> Compiler ──> 原生 MOSS-Image 节点
                                         ▲                                        │
                                         └────────── 生成图回接 Editor 当底图对比迭代 ──────────┘
```

上游 agent（可在其他设备运行 MOSS 官方 skill）产出的中文草稿，复制粘贴到 Editor 的 `source` 即可；也可以先用 ImageReverse 从图片反推出中文草稿。

## 核心功能

- **Editor：可视化画布**——拖拽/四角缩放/撤销重做、图层、重叠检测、比例与像素预设、16 倍数吸附、底图对比、实时校验；内置 📐 版式模板与文字容量提示。
- **Editor：紧凑布局**——LLM/AI 选项双列；两个姿势参考框双列且可拖右下角调整高度；`ai_prompt` 可拖拽拉伸；图层面板默认折行显示完整 caption。
- **Translator：中→英**——OpenAI 兼容 LLM；`reuse_translation` 可复用上次成功译文，反复出图不重复计费；粘贴已编译 JSON 时用 `canvas_width`/`canvas_height` 反解真实画布坐标。
- **Compiler：官方口径**——按 OpenMOSS/MOSS-Image 官方 skill 编译与严格校验；caption 输出纯文本，layout/已编译输出单行 JSON。
- **ImageReverse（可选）**——图片 → 中文散文 / 中文草稿 / 画布 JSON；四档模式，默认关闭。
- **姿势参考（可选）**——接 comfyui-pose-gallery 的 `description`，或手填/🎯 点选；支持 ⚡拆解展开与 📥本地短语注入。
- **服装参考（可选）**——接 comfyui-costume-gallery 的 `description`，或手填；支持 ⚡拆解展开与 📥服装本地短语注入；与姿势段共存。

## 快速开始

1. **安装**：把本目录放进 `ComfyUI/custom_nodes/`，重启 ComfyUI。插件不新增第三方依赖：HTTP/图像处理使用 ComfyUI 自带的 aiohttp / Pillow / numpy。
2. **配置模型**：复制 `config.example.json` 为 `config.json`，按 provider 填 `base_url`/`model`/`api_key`；也可直接在节点 widget 填（节点值优先）。
3. **主链接线**：`Editor.draft → Translator.draft → Compiler.draft → CLIPTextEncode/采样器`。caption 形态把 Editor 的 `width`/`height` 接 Compiler 的 `canvas_width`/`canvas_height`（推荐工作流已接好）。
4. **文字输入**：把中文散文或 layout 草稿贴进 **Editor** 的 `source`；散文点 **⚡拆解** 生成画布，layout 草稿会直接识别。
5. **接生成节点**：Compiler 的 `prompt`/`clip_text` 接正向 CLIPTextEncode，`negative` 接负向，`width`/`height` 接空 Latent；**不要再送 MOSS-Image Layout Prompt**。
6. **可选入口**：图片反推接 `LoadImage → ImageReverse → Editor.reverse_in`；姿势接 `Pose Gallery.description → Editor.pose_in(_2)`；服装接 `Costume Gallery.description → Editor.costume_in(_2)`。都不影响主链。

推荐工作流：[`examples/MOSS-Image-生图.json`](examples/MOSS-Image-生图.json)（Editor → Translator → Compiler → 采样的完整文生图流；示例基于 v1.7.1；旧版 Editor 节点请按顶部提示删除重建并重新连线）。

### 推荐工作流依赖

缺节点用 ComfyUI Manager 安装；模型可换官方 MOSS-Image 权重，但 CLIP/VAE 必须匹配。

| 类型 | 工作流中的文件/版本 | 放置/说明 |
|---|---|---|
| ComfyUI | 0.43+（工作流 core 节点记录 0.41–0.42） | 需要支持 `CLIPLoader` 的 `moss_image` 类型 |
| 本插件 | MOSS Prompt Studio v1.8.0+ | Editor / Translator / Compiler / ImageReverse |
| UNet | `rayMossArtshoot_sftTurboV1.safetensors` | `ComfyUI/models/unet/`；可替换为官方 MOSS-Image-Base/SFT，采样参数按模型卡调整 |
| CLIP | `moss/qwen3.5_9b_int8_convrot.safetensors` | `ComfyUI/models/clip/`，`CLIPLoader` 类型选 `moss_image` |
| VAE | `moss/flux2-vae.safetensors` | `ComfyUI/models/vae/` |
| rgthree-comfy | `8ff50e4…`（Seed；另含 Label / Fast Groups Bypasser） | Manager 安装，版本以工作流节点记录为准 |
| ComfyUI-Easy-Use | `b0b47ca…` / `005c578…`（easy cleanGpuUsed / easy showAnything） | Manager 安装 |
| TESpeedMoss | 工作流节点 218 | 按该节点包来源安装 |

### 目录结构

```
custom_nodes/comfyui-moss-prompt-studio/
├── nodes/ + py/        # 四个节点与后端核心
├── web/                # 画布/反推前端
├── agents/             # 可下拉选择的追加注入指令
├── AGENT.example.md    # 根目录注入文件模板
├── config.example.json # LLM 连接配置模板
├── examples/           # 推荐工作流 + 示例草稿
├── tests/              # 离线测试
└── LICENSE             # MIT
```

## 节点概览

| 节点 | 作用 |
|---|---|
| **Editor** | 中文草稿/反推结果的可视化画布编辑，输出 `draft` + `width`/`height` |
| **Translator** | 中文草稿 → 英文草稿（LLM；可开启译文缓存复用；已编译 JSON 需设置 `canvas_width`/`canvas_height`） |
| **Compiler** | 官方口径编译 + 严格校验 |
| **ImageReverse** | 图片 → 中文散文 / 中文草稿 / 画布 JSON（可选入口，`enabled` 默认关闭） |

> 最小主链只需 Editor → Translator → Compiler；ImageReverse 与姿势图库都是可选入口。

## Editor 使用要点

### 画布、模板与文字

- `source` 粘贴中文散文或 layout 草稿；`edits` 是自动生成、也可手改的布局 JSON。散文点 **⚡拆解** 生成画布，layout 草稿直接识别；普通 Queue 按输入哈希缓存，**⚡拆解** 每次强制重拆。
- **📐模板**：内置竖版海报 / 方形社媒 / 电商主图 / 杂志封面 / 人物介绍卡 / 四格分镜，载入后可 ↶撤销。
- 文字框过小或过长会在状态行给出 ⚠ 容量提示（启发式估算，仅提示、不拦截提交）。
- 图层行默认**折行显示完整 caption**，方便直接阅读简单的 JSON 画布；上移/下移/删除按钮保持右上角。
- 画布支持拖拽、四角缩放、方向键微调、滚轮缩放、重叠检测；比例/像素预设会保持当前面积并自动对齐 16 倍数。

### 双列布局与尺寸

- **LLM/AI 选项双列**：左列 `ai_draft`/`provider`/`base_url`/`model`/`api_key`，右列 `temperature`/`thinking`/`timeout`/`agent_md`；`ai_prompt` 横跨两列。
- **姿势参考双列**：`pose_in`/`pose_in_2` 并排显示；两个框都可拖右下角调整高度，节点会自动重新分配空间；有连线时输入框自动禁用（连线优先）。
- **服装参考双列**：`costume_in`/`costume_in_2` 紧随姿势面板；同样支持拖高、连线优先。新控件全部追加在姿势控件之后，旧工作流 `widgets_values` 不错位。
- `ai_prompt` 也可拖右下角调整高度；面板会随内容重新布局。
- 原标准 widget 被隐藏但仍参与工作流序列化，后端参数与 API 提交不变。
- **旧 Editor 节点请删除后重新添加并重新连线**；旧节点/旧连线的布局兼容不在本次保证范围内。

### 归属、撤销与反推写回

- `edits` 只对写入时的那份 `source` 生效（隐藏 `edits_source_sig` 记录归属）；换 `source` 后旧画布自动失效，以新 source 为准。
- 撤销/重做保留完整画布历史；📐模板载入会保留撤销点。
- 反推结果在执行结束后写回 Editor（prose/draft → source，canvas → edits），**随后再 Queue 一次**才会用新数据生成；同结果不覆盖手改。

## LLM 配置

| Provider | 默认地址 / 模型 | 说明 |
|---|---|---|
| `mimo` | 套餐地址 / `mimo-v2.6-flash` | MiMo Token Plan key |
| `deepseek` | `https://api.deepseek.com/v1` / `deepseek-chat`（官方别名，当前映射 flash） | 支持图片 |
| `minimax` | `https://api.minimax.cn/v1` / `MiniMax-M3.1-Flash` | M Plan/按量 key 不可混用；思考关不掉，off→low |
| `custom` | 任意 OpenAI 兼容 URL | LM Studio / Ollama / 网关；本地 `api_key=EMPTY` |

**Translator 的已编译输入**：`canvas_width`/`canvas_height`（默认 1024）只在粘贴已编译 JSON（`abs_centralized/yxyx`）时用于反解坐标；按原图尺寸填写，caption/草稿形态不影响译文。

- 复制 `config.example.json` → `config.json`；**widget 值优先于 config**。
- 三个 LLM 节点（Editor / Translator / ImageReverse）的连接配置各自独立；任一节点的 **⇄同步** 会把 `provider/base_url/model/temperature/thinking/timeout` 复制到另外两个节点，**`api_key` 不参与同步**。
- 建议 key 放 `config.json`，节点 `api_key` 保持 `EMPTY`，避免分享工作流时泄露。**widget 里的 `api_key` 会明文保存进工作流 JSON 和 ComfyUI 执行历史（`/history` 的 `prompt.inputs.api_key`）**；保存/分享工作流、导出历史前务必确认节点里没有真实 key。
- 只有目标 `base_url` 等于该 provider 的 config/官方默认地址时才回退配置 key；把 `base_url` 改成别的地址（含内网/攻击者地址）时必须显式填 key，否则不发 `Authorization`。`custom` 如需使用 config 里的 key，请同时在 config 的 `custom.base_url` 写入与节点一致的地址。
- 连接测试路由要求 ComfyUI 页面同源，跨源请求直接拒绝；连接测试与主 LLM 通道共用跨主机重定向防护：服务端 3xx 指向其他 host/port 时直接报错，`Authorization` 不会被转发。
- 连接测试默认拒绝 `localhost`/内网目标（即使请求来自同源页面）；需要测本地 Ollama / LM Studio 时在 `config.json` 顶层加 `"probe_allow_private": true`。
- 本地 Ollama 用 `provider=custom` + `http://127.0.0.1:11434/v1`，并设置 `OLLAMA_CONTEXT_LENGTH=32768`。反推的 custom 非 manual 模式输出预算自动压到 16384，避免 65536 超过本地 32K 上下文；manual 模式尊重手填值。
- `max_tokens=0` 表示“插件默认”（按 provider 由请求体组装逻辑决定），不是服务商默认值。
- Translator 的 `reuse_translation` 开启后，相同翻译输入（中文草稿/模型/思考/提示注入）复用上次成功译文，适合反复出图找最优图；关闭时强制重译并刷新缓存。

## 可选功能

### 图像反推（ImageReverse）

随插件安装，最小接法 `LoadImage → MOSS ImageReverse.image`。`enabled` 默认关闭；要用时接好 Editor 后勾选并 Queue，或点 **⚡反推** 单次执行。

`provider`/`base_url`/`model`/`api_key` 与 Editor/Translator 相同（留空读 `config.json`；本地 Ollama/LM Studio 用 `custom` + 准确模型 tag + `EMPTY`）。`reverse_mode` 提供 `concise`/`standard`/`detailed`/`manual` 四档：非 manual 使用预设，manual 才启用 temperature/thinking/max_items/max_tokens/image_detail/image_max_side 等细节控件。模式取值、预算与写回细节以节点 tooltip 为准；结果写回 Editor 后需再 Queue 一次。生图前建议关闭/旁路 `enabled` 的反推，避免完整 Queue 重跑并覆盖画布。

### 姿势参考（comfyui-pose-gallery）

**不接时 Editor / Translator / Compiler 与旧版行为完全一致**。需要姿势控制时：

1. 用 ComfyUI Manager 或按其 README 安装 [comfyui-pose-gallery](https://github.com/wangweihong/comfyui-pose-gallery) 到 `custom_nodes/`，重启 ComfyUI（本地中文姿势图库）；本插件只消费其 `description` 文本输出，不分发其图片/索引。
2. 接法：`Pose Gallery.description → Editor.pose_in`；双人时再接 `pose_in_2`。`pose_in`/`pose_in_2` 也支持手填和 🎯 点选（连线优先）。`pose_mode` 默认 `自动`。
3. 两种注入：**⚡拆解**（LLM 展开姿势，一次 LLM 调用）与 **📥姿势**（本地短语版，拆解后免费秒注入）；`pose_mode` 控制自动/强制双人/关闭；检查器可「🔄 对调两人姿势」。
4. 文字姿势约束弱于深度图，效果受模型影响；复杂姿势建议 ⚡拆解 + `thinking=low`，骨骼级控制请走 pose-gallery 的深度链路。具体控件与注入规则以节点 tooltip 为准。

> **pose-gallery 许可与免责**：其节点代码/画廊页面为 MIT；姿势图片、缩略图与原始数据不适用 MIT，权利归原作者 ギア（@giaaibusi），中文数据由 Anhao 整理（上游：wangweihong/ai-pose-gallery-zh、anhao/ai-pose-gallery-zh）。本插件不分发这些素材，仅运行时读取本地索引/缩略图；请阅读其 LICENSE/NOTICE 并保留署名。其 README 亦提示仅验证部分姿势组，手脚细节与精确关节角度可能偏离参考，请自行评估生成结果。

### 服装参考（comfyui-costume-gallery）

**不接时 Editor / Translator / Compiler 与旧版行为完全一致**。需要服装控制时：

1. 按 [comfyui-costume-gallery](https://github.com/wangweihong/comfyui-costume-gallery) 的 README 安装到 `custom_nodes/`，重启 ComfyUI。**该节点没有输出级惰性：即使只接 `description`，每次执行也会解码 PNG，`VALIDATE_INPUTS` 还要求原图存在**，因此必须安装完整图库（约 **1.42 GiB**，其中 `library/png/` 约 1.33 GiB）；不能只装索引。
2. 接法：`Costume Gallery.description → Editor.costume_in`；双人时再接第二个 Costume Gallery 的 `description → costume_in_2`。`costume_in`/`costume_in_2` 也支持手填（连线优先）。`costume_mode` 默认 `自动`，可选 `关闭` 做 A/B 对照。
3. 两种注入：**⚡拆解**（LLM 把关键词展开成完整服装描述，一次 LLM 调用）与 **📥服装**（本地短语版：`分类 / 关键词…` → `身穿…`，拆解后免费秒注入）；检查器可「🔄 对调两人服装」，与「对调两人姿势」互不影响。
   - **📥服装会移除与目标角色重叠的旧服装 `object` 条目**（反推草稿常把旧连衣裙/网袜单独拆成 object，不移除会被画回去）；不重叠的场景服装（橱窗里的裙子）与背景/配饰不受影响。
   - **注意混合**：📥服装只追加「；服装：」段；若画布 `summary`/角色描述里已写有旧衣服（反推草稿尤其常见），模型可能新旧混穿。检测到旧服装措辞时状态行会给出 ⚠；要彻底替换请用 ⚡拆解（LLM 会改写 summary 与角色服装）。
4. 分配规则：单参考给最突出角色；双参考按 bbox 左右分配（左→`costume_in`，右→`costume_in_2`）；也可在画布选中角色后只注入它。服装没有“双人整体”语义。
5. **不要**把 `Costume Gallery.image` 接到 `Editor.underlay`：underlay 是铺满画布的背景底图，不是 bbox 参考，而且会让 Editor 每次 Queue 都重跑。服装图只用于图库选择，不进主链路。
6. 敏感分类提示：图库中「内衣 / 性感 / 泳装」共 393 条（27.9%）。走云端 LLM/翻译时请自行确认内容合规；插件会在 ⚡拆解/📥服装 状态行给出警告，但**不拦截**。
7. 文字注入的服装保真度弱于图库自带的“三图参考换装”工作流（Qwen Image 2.1 图像参考）；本插件提供的是互补的文字控制能力，不承诺图库同款还原。具体控件与注入规则以节点 tooltip 为准。

> **costume-gallery 许可与免责**：其节点代码为 MIT（`LICENSE` 含 Scope 条款：仅覆盖代码/脚本；GitHub 因此识别为 `NOASSERTION`），服装图片、缩略图与原始数据不适用 MIT；原始素材作者 ギア（@giaaibusi），中文整理/上游 Deeyu（wangweihong 维护节点与桥接）。本插件不分发其素材，仅消费用户本地已安装节点的 `description` 文本输出；请阅读其 `LICENSE`、`NOTICE.md`、`THIRD_PARTY_NOTICES.md` 并保留署名。

## 输入格式（上游契约）

上游 agent 的产物：中文 layout 草稿（`abs`/`xyxy`、宽高 16 倍数）或中文散文；画面可见文字在 `text` 字段原样保留。整段粘贴即可，不需要英文版或备注文件。

## 提示词注入

四个注入口可叠加：`AGENT.md`/`agent_md`（追加持久指令）、`system_prompt`（整体改写契约）、`extra_instructions`（连线逐次指令）、`extra_body`（最后合并请求体）。节点内可直接编辑，具体规则见 tooltip。

## 测试

```bash
python tests/run_tests.py     # 102 用例
node --test tests/*.test.mjs  # 128 用例（glob 自动包含所有前端测试）
```

覆盖编译/识别/翻译/反推/连接测试/姿势注入/服装注入，以及前端纯逻辑、生命周期、写回和模式预设。控制台会打印 `[MOSS Studio] LLM ...` 日志，可用于排查耗时、重试和截断。真实 LLM 的姿势注入对照实验：`python tests/bench_ai_draft.py --variants pose_solo,pose_dual,pose_pair`（需 config.json）。

## 版本与更新

- **v1.8.0**：新增可选服装注入（comfyui-costume-gallery `description → costume_in(_2)`、`costume_mode`、📥服装、🔄 对调两人服装）；caption 改为通用分段解析，姿势/服装/遮挡后缀可共存且互不删除；修复连续快速注入的结果误判（submit token）与反推草稿旧服装 object 残留；新增敏感分类/旧服装混合提示；⚡拆解 改为主色大按钮；Editor/Translator 画布宽高控件类型归一（旧工作流错位导致的"运行成功但不出图"）。Python **102** / Node **128**。
- **v1.7.2**：Editor 布局更新（LLM/AI 选项双列、姿势框双列可调高、`ai_prompt` 可拉伸、图层全文折行）；Translator 译文缓存与非 1024 已编译输入（`canvas_width`/`canvas_height`）；P0 官方契约修正（caption 纯文本、空 `meta/tags/palette`、非 text `text`、bbox 精度）；修复 Python/JS 验证器分叉、队列结果归属/重复卡死/事务写回、SSRF 100.64/10、探测错误体限长、`agent_md` 删除锁死、检查器焦点、画布高度与指针清理。Python **90** / Node **112**。
- **v1.7.1**：新增可选姿势注入节点并加固连接测试、缓存与资源生命周期。

## 设计依据与致谢

- 编译器与校验规则移植自 [OpenMOSS/MOSS-Image](https://github.com/OpenMOSS/MOSS-Image) 官方 skill（`skills/moss-image-prompt-enhancer`），保证与官方口径一致。
- 画布交互改编自 [swan7-py/json-prompt-visualizer](https://github.com/swan7-py/json-prompt-visualizer) 的 canvas.py（拖拽/缩放/撤销/重叠闸门/坐标换算思路），适配为 ComfyUI 前端扩展。
- 指令文件的追加注入语义参考了 [Codex-X](https://github.com/yynxxxxx/Codex-X) 的 Append 托管块设计。
- 姿势图库集成与中文姿势数据来自 [wangweihong/comfyui-pose-gallery](https://github.com/wangweihong/comfyui-pose-gallery)；感谢开发者 wangweihong，以及原始姿势画作者 ギア（@giaaibusi）与中文数据整理 Anhao。
- 服装图库集成与中文服装数据来自 [wangweihong/comfyui-costume-gallery](https://github.com/wangweihong/comfyui-costume-gallery)；感谢 wangweihong、Deeyu 与原始素材作者 ギア（@giaaibusi）。

## 使用须知与免责声明

本项目只提供提示词编辑、翻译和格式转换，本身不生成、不托管、不审核图像内容。请仅用于合法目的，并自行确保输入、生成内容和发布行为符合适用法律法规、目标平台条款及第三方模型/API 政策；API key 由你自行保管。

本项目可选集成 comfyui-pose-gallery 与 comfyui-costume-gallery：其节点代码为 MIT（costume-gallery 的 `LICENSE` 含仅覆盖代码的 Scope 条款），姿势/服装图片、缩略图与原始数据不适用 MIT（权利归原作者 ギア，中文数据分别由 Anhao / Deeyu 整理）；本插件不分发其素材，使用者需自行遵守其 LICENSE/NOTICE 与上游条款。

严禁用于制作或传播违法内容（包括 CSAM、非自愿色情、暴力、仇恨、骚扰、恐怖主义、诈骗、虚假信息，以及侵犯他人合法权益的内容），严禁绕过平台安全机制或冒充他人。AI 输出可能不准确或不合适，请自行审核并对使用结果负责；在适用法律允许的范围内，作者与贡献者不对任何直接或间接损失、法律责任或第三方索赔负责。本项目按 MIT 许可“按原样”提供，不附带任何担保；本声明不构成法律建议。

> For lawful prompt engineering only. Users are solely responsible for their inputs, outputs, and compliance with applicable laws and third-party terms. No warranty; no liability for misuse.

## 许可

MIT（见 [LICENSE](LICENSE)）。
