# MOSS Prompt Studio（ComfyUI 插件）

当前版本：**v1.6.0**

> **v1.6.0 更新**：1）新增 MiniMax M3.1 Flash 兼容（provider 预设/连接测试）；2）新增图像反推节点：图片→中文散文/草稿/画布 JSON，四模式可调，可选入口不接也能用。

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

推荐工作流：[`examples/MOSS-Image-生图.json`](examples/MOSS-Image-生图.json)（完整的 Editor → Translator → Compiler → CLIPTextEncode → KSampler 文生图流）。

**推荐工作流依赖**（缺节点用 ComfyUI Manager 安装；模型可换官方 MOSS-Image 权重，但 CLIP/VAE 必须匹配）：

| 类型 | 工作流中的文件/版本 | 放置/说明 |
|---|---|---|
| ComfyUI | 0.43+（工作流 core 节点记录 0.41–0.42） | 需要支持 `CLIPLoader` 的 `moss_image` 类型 |
| 本插件 | MOSS Prompt Studio v1.6.0+ | Editor / Translator / Compiler / ImageReverse |
| UNet | `rayMossArtshoot_sftTurboV1.safetensors` | `ComfyUI/models/unet/`；可替换为官方 MOSS-Image-Base/SFT，采样参数按模型卡调整 |
| CLIP | `moss/qwen3.5_9b_int8_convrot.safetensors` | `ComfyUI/models/clip/`，`CLIPLoader` 类型选 `moss_image` |
| VAE | `moss/flux2-vae.safetensors` | `ComfyUI/models/vae/` |
| rgthree-comfy | `8ff50e4…`（Seed；另含 Label / Fast Groups Bypasser） | Manager 安装，版本以工作流节点记录为准 |
| ComfyUI-Easy-Use | `b0b47ca…` / `005c578…`（easy cleanGpuUsed / easy showAnything） | Manager 安装 |
| TESpeedMoss | 工作流节点 218 | 按该节点包来源安装 |

## 安装

把本目录放进 `ComfyUI/custom_nodes/`（zip 解压或 git 均可），重启 ComfyUI。插件不新增第三方依赖：HTTP/图像处理使用 ComfyUI 自带的 aiohttp / Pillow / numpy。

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

> 节点参数、反推四模式和 manual 细节参数都以节点 tooltip 为准，这里不重复。图像反推会把图片发送到所选 LLM 端点；敏感图片用本地 `custom`。

## 节点概览

| 节点 | 作用 |
|---|---|
| **Editor** | 中文草稿/反推结果的可视化画布编辑，输出 `draft` + `width`/`height` |
| **Translator** | 中文草稿 → 英文草稿（LLM） |
| **Compiler** | 官方口径编译 + 严格校验 |
| **ImageReverse** | 图片 → 中文散文 / 中文草稿 / 画布 JSON（可选入口，`enabled` 默认关闭） |

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
- 本地 Ollama 用 `provider=custom` + `http://127.0.0.1:11434/v1`，并设置 `OLLAMA_CONTEXT_LENGTH=32768`。反推的 custom 非 manual 模式输出预算自动压到 16384，避免 65536 超过本地 32K 上下文；manual 模式尊重手填值。
- `max_tokens=0` 表示“插件默认”（按 provider 由请求体组装逻辑决定），不是服务商默认值。
- Editor 的 `edits` 只对写入时的那份 `source` 生效（隐藏 `edits_source_sig` 记录归属）；换 `source` 后旧画布自动失效、以新 source 为准。AI 拆解普通 Queue 按输入哈希缓存，重复 Queue 不重复计费；工具栏 **⚡拆解** 每次强制重拆。

## 提示词注入

四个注入口可叠加：`AGENT.md`/`agent_md`（追加持久指令）、`system_prompt`（整体改写契约）、`extra_instructions`（连线逐次指令）、`extra_body`（最后合并请求体）。节点内可直接编辑，具体规则见 tooltip。

## 测试

```bash
python tests/run_tests.py
node --test tests/model.test.mjs tests/hooks.test.mjs tests/reverse.test.mjs tests/llm_config.test.mjs tests/reverse_modes.test.mjs
```

覆盖编译/识别/翻译/反推/连接测试，以及前端纯逻辑、生命周期、写回和模式预设。控制台会打印 `[MOSS Studio] LLM ...` 日志，可用于排查耗时、重试和截断。

## 上游契约

给上游 agent 的产物：中文 layout 草稿（`abs`/`xyxy`、宽高 16 倍数）或中文散文；画面可见文字在 `text` 字段原样保留。整段粘贴即可，不需要英文版或备注文件。

## 设计依据与致谢

- 编译器与校验规则移植自 [OpenMOSS/MOSS-Image](https://github.com/OpenMOSS/MOSS-Image) 官方 skill（`skills/moss-image-prompt-enhancer`），保证与官方口径一致。
- 画布交互改编自 [swan7-py/json-prompt-visualizer](https://github.com/swan7-py/json-prompt-visualizer) 的 canvas.py（拖拽/缩放/撤销/重叠闸门/坐标换算思路），适配为 ComfyUI 前端扩展。
- 指令文件的追加注入语义参考了 [Codex-X](https://github.com/yynxxxxx/Codex-X) 的 Append 托管块设计。

## 使用须知与免责声明

本项目只提供提示词编辑、翻译和格式转换，本身不生成、不托管、不审核图像内容。请仅用于合法目的，并自行确保输入、生成内容和发布行为符合适用法律法规、目标平台条款及第三方模型/API 政策；API key 由你自行保管。

严禁用于制作或传播违法内容（包括 CSAM、非自愿色情、暴力、仇恨、骚扰、恐怖主义、诈骗、虚假信息，以及侵犯他人合法权益的内容），严禁绕过平台安全机制或冒充他人。AI 输出可能不准确或不合适，请自行审核并对使用结果负责；在适用法律允许的范围内，作者与贡献者不对任何直接或间接损失、法律责任或第三方索赔负责。本项目按 MIT 许可“按原样”提供，不附带任何担保；本声明不构成法律建议。

> For lawful prompt engineering only. Users are solely responsible for their inputs, outputs, and compliance with applicable laws and third-party terms. No warranty; no liability for misuse.

## 许可

MIT（见 [LICENSE](LICENSE)）。
