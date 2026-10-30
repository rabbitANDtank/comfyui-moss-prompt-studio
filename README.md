# MOSS Prompt Studio（ComfyUI 插件）

当前版本：**v1.7.1**

> **v1.7.1 更新**：新增可选姿势注入（⚡拆解、📥本地、🎯图库点选，需另装 pose-gallery）；修复旧源迟到结果覆盖画布、Queue 卡死与旧工作流空值；阻止跨主机 302 转发 API key，并加固连接测试、缓存与资源生命周期。

把上游 agent 产出的**中文 MOSS-Image 提示词草稿或散文式提示词**，在 ComfyUI 里可视化编辑、翻译成英文、按官方口径编译，直接接 ComfyUI 原生 MOSS-Image 节点生图。

```
[其他设备] agent + MOSS官方skill ──产──> 中文草稿 JSON (caption 或 layout)
        │ 手动复制粘贴到 Editor 节点
        ▼
LoadImage ──> MOSSImageReverse (可选) ──prose/draft/canvas──> MOSSEditor ──draft_zh──> MOSSTranslator ──draft_en──> MOSSCompiler
             (视觉 LLM 反推)                          (画布中文编辑)          (LLM 中→英)                  (官方编译器移植)
                                                                                    ▲ Ctrl+B 可旁路             │prompt/negative/width/height
                                                                                                                ▼
                                                                                               ComfyUI 原生 MOSS-Image 节点
                                                                                                                │
                                                                                                 生成图可回接 Editor 当底图对比迭代
```

> caption 形态没有框，草稿 JSON 里不含尺寸：把 Editor 的 `width`/`height` 输出接到 Compiler 的 `canvas_width`/`canvas_height` 输入，生图尺寸才会与画布一致（推荐工作流已接好）。layout 形态的尺寸随草稿自动传递，无需额外连线。

推荐工作流：[`examples/MOSS-Image-生图.json`](examples/MOSS-Image-生图.json)（完整的 Editor → Translator → Compiler → CLIPTextEncode → KSampler 文生图流）。示例中的 Editor 已补齐 v1.7.1 的 22 项 widget 默认值（含姿势控件），旧工作流加载后也会自动归一化。

**推荐工作流依赖**（缺节点用 ComfyUI Manager 安装；模型可换官方 MOSS-Image 权重，但 CLIP/VAE 必须匹配）：

| 类型 | 工作流中的文件/版本 | 放置/说明 |
|---|---|---|
| ComfyUI | 0.43+（工作流 core 节点记录 0.41–0.42） | 需要支持 `CLIPLoader` 的 `moss_image` 类型 |
| 本插件 | MOSS Prompt Studio v1.7.1+ | Editor / Translator / Compiler / ImageReverse |
| UNet | `rayMossArtshoot_sftTurboV1.safetensors` | `ComfyUI/models/unet/`；可替换为官方 MOSS-Image-Base/SFT，采样参数按模型卡调整 |
| CLIP | `moss/qwen3.5_9b_int8_convrot.safetensors` | `ComfyUI/models/clip/`，`CLIPLoader` 类型选 `moss_image` |
| VAE | `moss/flux2-vae.safetensors` | `ComfyUI/models/vae/` |
| rgthree-comfy | `8ff50e4…`（Seed；另含 Label / Fast Groups Bypasser） | Manager 安装，版本以工作流节点记录为准 |
| ComfyUI-Easy-Use | `b0b47ca…` / `005c578…`（easy cleanGpuUsed / easy showAnything） | Manager 安装 |
| TESpeedMoss | 工作流节点 218 | 按该节点包来源安装 |

## 安装

把本目录放进 `ComfyUI/custom_nodes/`（zip 解压或 git 均可），重启 ComfyUI。插件不新增第三方依赖：HTTP/图像处理使用 ComfyUI 自带的 aiohttp / Pillow / numpy。

主链 3 个节点（Editor / Translator / Compiler）开箱即用；**ImageReverse 与可选的 comfyui-pose-gallery 都是可选入口**，不装/不接不影响主链。配置方法见下文「可选节点配置」。

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

## 快速使用

1. **配置模型**：复制 `config.example.json` 为 `config.json`，按 provider 填 `base_url`/`model`/`api_key`；也可以直接在节点 widget 里填（节点值优先）。
2. **文字输入**：把中文散文或 layout 草稿贴进 **Editor** 的 `source`；散文点 **⚡拆解** 生成画布，layout 草稿会直接识别。
3. **可选：图片输入**：`LoadImage → MOSS 图像反推 → Editor.reverse_in`。反推节点的 `enabled` **默认关闭**；关闭时完整 Queue 不会调用视觉模型。要用反推：勾选 `enabled` 后 Queue（完整链路会执行反推），或点节点上的 **⚡反推** 单独执行（只提交反推节点及其上游、本次强制启用，不改勾选）。反推结果在本次执行结束后写回前端 Editor，因此**完整链路需要再 Queue 一次**。不用图片反推时跳过本步，直接把文字/草稿贴进 Editor 的 `source`。
4. **完整链路**：`Editor.draft → Translator.draft → Compiler.draft → CLIPTextEncode/采样器`。caption 形态把 Editor 的 `width`/`height` 接 Compiler 的 `canvas_width`/`canvas_height`（推荐工作流已接好）。
5. **接生成节点**：Compiler 的 `prompt`/`clip_text` 接正向 CLIPTextEncode，`negative` 接负向，`width`/`height` 接空 Latent；不要再送 MOSS-Image Layout Prompt。
6. **反推写回（两步）**：本次反推执行的 `executed` 事件把结果写到前端 `prose_zh → source`；`draft_zh → source 且 ai_draft=false`；`canvas_zh → edits` 直接出画布。**随后再 Queue 一次**，Editor/Translator/Compiler 才会用新数据生成。同一结果不覆盖手改；`replace_edits=false` 时首次接反推若画布已有内容也会保留并提示，打开后重新 Queue 可强制写入。

> **LLM 反推是可选入口**：主链 Editor → Translator → Compiler 不依赖 ImageReverse。反推节点的 `enabled` 默认关闭，且前端在检测不到 Editor 的 `reverse_in` 连接时会自动把它关掉；关闭时即使节点被完整 Queue 执行也立即返回、不调用 LLM。需要时连好 Editor 再勾选 `enabled`（随完整链路跑），或点 **⚡反推**（单独跑反推及上游，本次强制启用）。暂时不用可 Ctrl+B 旁路。

## 可选节点配置

> 主链只需 Editor → Translator → Compiler。下面的 ImageReverse 与姿势图库都是可选入口，不装/不接不影响主链。

### MOSS 图像反推（ImageReverse）

节点随插件安装，无额外依赖；最小接法 `LoadImage → MOSS ImageReverse.image`。常用配置：

| 控件 | 默认 / 建议 | 说明 |
|---|---|---|
| `enabled` | `false` | 默认关闭；关闭时完整 Queue 不调用视觉模型。要用：接好 Editor 后勾选并 Queue，或点节点上的 **⚡反推** 单次执行（本次强制启用，不改勾选）。 |
| `provider` / `base_url` / `model` / `api_key` | 同 Editor/Translator | 留空读 `config.json`；本地 Ollama/LM Studio 用 `custom` + `http://127.0.0.1:11434/v1` 或 `http://127.0.0.1:1234/v1` + 准确模型 tag + `api_key=EMPTY`。 |
| `reverse_mode` | `standard` | `concise` / `standard` / `detailed` / `manual`；只有 `manual` 会显示并生效下方细节控件。 |
| `canvas_width` / `canvas_height` | `0` | `0` = 按输入图尺寸对齐 16；也可填与 Editor 画布一致的尺寸。 |
| `timeout` | `180` | 本地大模型首次加载可调 300–600。 |
| `system_prompt` / `agent_md` / `extra_instructions` / `extra_body` | 可选 | 四个注入口：整体契约 / 追加文件 / 连线逐次指令 / 原始请求体合并。 |

四模式预设（非 `manual` 固定生效，不受下方细节控件影响）：

| 模式 | 元素数 | 画面文字 | caption | max_tokens | image_detail | 最长边 |
|---|---|---|---|---|---|---|
| `concise` | 4 | 关闭 | low | 8192 | medium_low | 1024 |
| `standard` | 6 | 允许 | medium_low | 65536 | high | 1536 |
| `detailed` | 10 | 允许 | medium_high | 65536 | high | 1728 |
| `manual` | 1–30 | 手动 | 手动 | 0=插件默认 | 手动 | 512–2000 |

- `manual` 额外可调：`include_text`、`caption_detail`、`tag_detail`、`max_tokens`、`image_detail`、`image_max_side`、`thinking`、`temperature`、`max_items`。
- 非 manual 模式固定 `thinking=off`；`temperature` 按模式/provider 预设（DeepSeek/MiMo/custom：concise 0.1 / standard 0.2 / detailed 0.3；MiniMax 固定 1）。
- `max_tokens=0` 表示插件默认（按 provider 决定，不是服务商默认）；`custom` 非 manual 自动压到 16384，避免本地 32K 上下文超限。
- 反推结果写回：`ImageReverse.prose_zh/draft_zh/canvas_zh → Editor.reverse_in`（可选）；本次执行结束后写回前端，**再 Queue 一次** 才会用新数据生成。`enabled` 的反推在完整 Queue 中会重跑，生图前建议关闭/旁路（详见下方姿势章节的“用完即关”）。
- 敏感图片请用本地 `custom` 端点；图片会发送到所选 LLM。

### comfyui-pose-gallery（可选第三方）

- 安装：ComfyUI Manager 搜索 `comfyui-pose-gallery` 或按其 README 解压到 `custom_nodes/`，重启 ComfyUI；本插件**不分发**其图片/索引。
- 接线：`Pose Gallery.description → Editor.pose_in`；双人时再接 `pose_in_2`。也可不装图库，直接在 `pose_in` 手填或在 🎯 面板点选。
- `pose_mode` 设 `自动`（默认）；单人/双人分配、⚡拆解/📥姿势 用法见下方「姿势参考」。
- 许可与免责：代码/画廊页面 MIT；姿势图片、缩略图与原始数据**不适用 MIT**，权利归原作者 ギア（@giaaibusi），中文数据由 Anhao 整理；请遵守其 LICENSE/NOTICE。

## 姿势参考（可选：接 comfyui-pose-gallery）

**Editor 的 `pose_in` / `pose_in_2` 是可选输入：不接入时 Editor / Translator / Compiler 与旧版行为完全一致**（有回归测试保证 system prompt 逐字节不变）。需要丰富单人/双人姿势时：

1. 另装 [comfyui-pose-gallery](https://github.com/wangweihong/comfyui-pose-gallery)（本地中文姿势图库：1,439 张线稿，含 275 张双人姿势）。本插件只消费其 `description` 文本输出（形如 `全身单人・标准系 / 跪立 / 四肢着地・单腿后高抬 / 俯视`），不携带其任何图片/索引数据（许可与免责见本节末）。
2. 连线（可选）：`Pose Gallery.description → Editor.pose_in`；双人分别指定姿势时再接 `pose_in_2`。v1.7.1 起这两个输入也是可手填/点选的 widget（**连线值优先**），不装图库也能直接在 widget 里写动作描述。零成本福利：`Pose Gallery.image → Editor.underlay` 可把姿势线稿当底图，画 bbox 时直接对齐姿势轮廓——底图是**拉伸铺满**画布的，pose 图（3:4 竖屏）里人物的左右/上下/前后分布与画布坐标一一对应；想完全无形变，把画布宽高设成 3:4 的 16 倍数（如 896×1184、960×1280）。
3. 两个注入入口，各干各的活：
   - **⚡拆解（LLM 丰富版）**：连着姿势拆解，LLM 把姿势短语展开成具体肢体动作句写进角色 caption（一次 LLM 调用，收费）。换姿势后重点一次 ⚡拆解 即重新应用。**姿势与散文动作强冲突时（如散文"站立"注入"仰卧/跪立"），建议同时把 `thinking` 开到 `low`**：真机实测 off 档偶发忽略姿势参考（写成散文动作），low 档遵循显著更稳。
   - **📥姿势（本地短语版，拆解完成后随时用）**：先用 **🎯姿势①/②** 打开图库点选面板（体系/分类/视角筛选 + 搜索 + 缩略图 + ↻刷新索引；选中写入 `pose_in`/`pose_in_2`），再点画布工具栏 **📥姿势**，图库原句以 `；姿势：姿势名称（视角）` 段写入角色 caption（如 `；姿势：四肢着地・单腿后高抬（俯视）`——视角是参考线稿的观察角度，帮助模型还原肢体朝向，不会被转写成镜头指令）——不调 LLM、免费、秒完成，重复注入是替换不是叠加，写完可在检查器手改。注入只改 caption 姿势段，**不会动你手动拖过的画布布局**。注意短语约束较粗：真机 5 姿势实测 4/5 直接生效，"仰卧"这类与场景先验强冲突的复杂姿势建议走 ⚡拆解丰富版（必要时 `thinking=low`），或手改 caption 补充细节；要骨骼级精确控制请配 pose-gallery 的深度控制工作流（文字姿势约束天然弱于深度图）。
4. 📥姿势 的注入目标：**选中角色→只注入它**；未选中→双参考按 bbox 左右分配（`pose_in`→左、`pose_in_2`→右，上下/前后构图同理）、双人系列姿势（体系含"双人"）给两人各写一段互动描述、单姿势给最突出的角色。
5. `pose_mode` 闸门：`自动`（默认，拆解 LLM 按人数套用）/`强制双人`（散文人数含糊时手动确认）/`关闭`（忽略姿势输入，免拔线做 A/B 对照；📥姿势 按钮也会被拒绝）。
6. 画布上交换两人的姿势：选中任一角色条目，检查器出现 **🔄 对调两人姿势**——只交换两个 caption 末尾的姿势段，不重拆、不调 LLM、支持撤销。左右/上下/前后构图通用。
7. 注意：普通 Queue 不注入、不拆解（按输入哈希缓存）；⚡拆解/📥姿势 按钮都会自动带上姿势节点等上游一起提交，但**会自动剔除反推链上游**（避免反推被搭车重跑重复计费、以及它写回的新散文让本次拆解/注入画布立即失效回退）。反推产物同样适用：prose_zh 落 source 后走 ⚡拆解，draft_zh/canvas_zh 落好的画布随时可 📥姿势。`pose_in` 也可以接任意手写的动作描述文本，不依赖 pose-gallery。
   - **反推"用完即关"**：拆解/画布就绪后准备生图时，请把反推节点 `enabled` 关掉（或旁路）。真机踩坑：完整 Queue 会重跑 enabled 的反推，它写回的新散文会让当前画布立即回退成 caption 文本（画面上"只剩文字没有框"）；改过反推参数（画布尺寸/模式等）时签名变化，同图也视为新结果照样写回。同图同参的重放已被前端自动跳过（不覆盖已有成果），参数变了就不会跳过。
8. 姿势源与旧工作流兼容（v1.7.1）：`pose_in`/`pose_in_2` 现在是字符串 widget——可 🎯 点选、手填动作描述，或连线任意 STRING 节点（Pose Gallery.description / PrimitiveString）；**连线值优先于 widget**，有连线时点选会先询问是否断开。ComfyUI 原生 `PrimitiveNode` 现在也可以给该 widget 供值。加载 v1.7 之前的工作流时，空的 `pose_mode`/`apply_pose` 会自动回落到「自动」/-1，无需手动改。若 `pose_in`/`pose_in_2` 连的是 `PrimitiveString` 这类可写字符串节点，🎯点选会直接写入该节点并自动命名为「姿势人物①/②」，**连线保留**；连的是 Pose Gallery 等计算输出（不可写）才提示断开。也**不要把 MOSS 反推节点的输出当姿势源**——它每次执行都会触发事件写回，与拆解/注入画布互相竞争导致画布回退。

> **pose-gallery 许可与免责**：其节点代码/画廊页面为 MIT；姿势图片、缩略图与原始数据不适用 MIT，权利归原作者 ギア（@giaaibusi），中文数据由 Anhao 整理（上游：wangweihong/ai-pose-gallery-zh、anhao/ai-pose-gallery-zh）。本插件不分发这些素材，仅运行时读取本地索引/缩略图；请阅读其 LICENSE/NOTICE 并保留署名。其 README 亦提示仅验证部分姿势组，手脚细节与精确关节角度可能偏离参考，请自行评估生成结果。

## 节点概览

| 节点 | 作用 |
|---|---|
| **Editor** | 中文草稿/反推结果的可视化画布编辑，输出 `draft` + `width`/`height` |
| **Translator** | 中文草稿 → 英文草稿（LLM） |
| **Compiler** | 官方口径编译 + 严格校验 |
| **ImageReverse** | 图片 → 中文散文 / 中文草稿 / 画布 JSON（可选入口，`enabled` 默认关闭） |

> 四个节点均为可选组合：最小主链只需 Editor → Translator → Compiler；ImageReverse 与姿势图库都是可选入口，不装/不接不影响主链。

三个 LLM 节点（Editor / Translator / ImageReverse）的连接配置各自独立；任一节点的 **⇄同步** 会把 `provider/base_url/model/temperature/thinking/timeout` 复制到另外两个节点，**`api_key` 不参与同步**。想三个一样就同步（key 需各自配置或统一放 `config.json`），想三个不同就分别设置。

## 配置

| Provider | 默认地址 / 模型 | 说明 |
|---|---|---|
| `mimo` | 套餐地址 / `mimo-v2.6-flash` | MiMo Token Plan key |
| `deepseek` | `https://api.deepseek.com/v1` / `deepseek-chat`（官方别名，当前映射 flash） | 支持图片 |
| `minimax` | `https://api.minimax.cn/v1` / `MiniMax-M3.1-Flash` | M Plan/按量 key 不可混用；思考关不掉，off→low |
| `custom` | 任意 OpenAI 兼容 URL | LM Studio / Ollama / 网关；本地 `api_key=EMPTY` |

- 复制 `config.example.json` → `config.json`；**widget 值优先于 config**。
- 建议 key 放 `config.json`，节点 `api_key` 保持 `EMPTY`，避免分享工作流时泄露。**注意：widget 里的 `api_key` 会明文保存进工作流 JSON 和 ComfyUI 执行历史（`/history` 的 `prompt.inputs.api_key`）**；保存/分享工作流、导出历史前务必确认节点里没有真实 key。只有目标 `base_url` 等于该 provider 的 config/官方默认地址时才回退配置 key；把 `base_url` 改成别的地址（含内网/攻击者地址）时必须显式填 key，否则不发 `Authorization`。`custom` 如需使用 config 里的 key，请同时在 config 的 `custom.base_url` 写入与节点一致的地址。连接测试路由还要求 ComfyUI 页面同源，跨源请求直接拒绝。
- 连接测试与主 LLM 通道共用跨主机重定向防护：服务端 3xx 指向其他 host/port 时直接报错，`Authorization` 不会被转发。连接测试默认拒绝 `localhost`/内网目标（即使请求来自同源页面）；需要测本地 Ollama/LM Studio 时在 `config.json` 顶层加 `"probe_allow_private": true`。
- 本地 Ollama 用 `provider=custom` + `http://127.0.0.1:11434/v1`，并设置 `OLLAMA_CONTEXT_LENGTH=32768`。反推的 custom 非 manual 模式输出预算自动压到 16384，避免 65536 超过本地 32K 上下文；manual 模式尊重手填值。
- `max_tokens=0` 表示“插件默认”（按 provider 由请求体组装逻辑决定），不是服务商默认值。
- Editor 的 `edits` 只对写入时的那份 `source` 生效（隐藏 `edits_source_sig` 记录归属）；换 `source` 后旧画布自动失效、以新 source 为准。AI 拆解普通 Queue 按输入哈希缓存，重复 Queue 不重复计费；工具栏 **⚡拆解** 每次强制重拆。

## 提示词注入

四个注入口可叠加：`AGENT.md`/`agent_md`（追加持久指令）、`system_prompt`（整体改写契约）、`extra_instructions`（连线逐次指令）、`extra_body`（最后合并请求体）。节点内可直接编辑，具体规则见 tooltip。

## 测试

```bash
python tests/run_tests.py     # 78 用例
node --test tests/*.test.mjs  # 75 用例（glob 自动包含所有前端测试）
```

覆盖编译/识别/翻译/反推/连接测试/姿势注入，以及前端纯逻辑、生命周期、写回和模式预设。控制台会打印 `[MOSS Studio] LLM ...` 日志，可用于排查耗时、重试和截断。真实 LLM 的姿势注入对照实验：`python tests/bench_ai_draft.py --variants pose_solo,pose_dual,pose_pair`（需 config.json）。

## 上游契约

给上游 agent 的产物：中文 layout 草稿（`abs`/`xyxy`、宽高 16 倍数）或中文散文；画面可见文字在 `text` 字段原样保留。整段粘贴即可，不需要英文版或备注文件。

## 设计依据与致谢

- 编译器与校验规则移植自 [OpenMOSS/MOSS-Image](https://github.com/OpenMOSS/MOSS-Image) 官方 skill（`skills/moss-image-prompt-enhancer`），保证与官方口径一致。
- 画布交互改编自 [swan7-py/json-prompt-visualizer](https://github.com/swan7-py/json-prompt-visualizer) 的 canvas.py（拖拽/缩放/撤销/重叠闸门/坐标换算思路），适配为 ComfyUI 前端扩展。
- 指令文件的追加注入语义参考了 [Codex-X](https://github.com/yynxxxxx/Codex-X) 的 Append 托管块设计。

## 使用须知与免责声明

本项目只提供提示词编辑、翻译和格式转换，本身不生成、不托管、不审核图像内容。请仅用于合法目的，并自行确保输入、生成内容和发布行为符合适用法律法规、目标平台条款及第三方模型/API 政策；API key 由你自行保管。

本项目可选集成 comfyui-pose-gallery：其代码/画廊页面为 MIT，姿势图片、缩略图与原始数据不适用 MIT（权利归原作者 ギア，中文数据由 Anhao 整理）；本插件不分发其素材，使用者需自行遵守其 LICENSE/NOTICE 与上游条款。

严禁用于制作或传播违法内容（包括 CSAM、非自愿色情、暴力、仇恨、骚扰、恐怖主义、诈骗、虚假信息，以及侵犯他人合法权益的内容），严禁绕过平台安全机制或冒充他人。AI 输出可能不准确或不合适，请自行审核并对使用结果负责；在适用法律允许的范围内，作者与贡献者不对任何直接或间接损失、法律责任或第三方索赔负责。本项目按 MIT 许可“按原样”提供，不附带任何担保；本声明不构成法律建议。

> For lawful prompt engineering only. Users are solely responsible for their inputs, outputs, and compliance with applicable laws and third-party terms. No warranty; no liability for misuse.

## 许可

MIT（见 [LICENSE](LICENSE)）。
