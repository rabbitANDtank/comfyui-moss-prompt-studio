# AGENT.md — 持久化指令（追加注入）

两种用法：
1. **根目录默认**：把本文件复制为 `AGENT.md` 放在插件根目录（与 config.json 同级），
   节点 `agent_md` 下拉保持"(默认 AGENT.md)"即自动注入；
2. **多套切换**：把 .md 文件放进 `agents/` 目录，在编辑器/翻译器节点的 `agent_md`
   下拉框里选择（像 LoadImage 选图片一样；新文件刷新页面后出现，改动文件下次执行自动生效）。

内容会以托管块标记包裹后**追加**在翻译器与 AI 拆解的系统提示词末尾——不替换默认契约、
不替换 system_prompt / ai_prompt widget，与其他注入口可叠加。

规则：
- 文件不存在或为空 = 不注入，节点行为与原来完全一致
- 文件里**没有**托管标记时，**整个文件**都会被注入（删掉说明写你自己的内容即可）
- 文件里**有**一对托管标记时，只注入 BEGIN/END 之间的片段，标记外的文字（包括本段
  说明）一律不注入；两个标记需各恰好出现一次且 BEGIN 在前，否则节点直接报错。
  真实标记是 HTML 注释形式，本文件末尾就有一对现成的可以照抄。
  示意（故意用假名、去掉注释符号，防止本文件自己的说明被解析成标记）：

```
这里是我的笔记，不会被注入
MOSS-STUDIO:AGENT-BEGIN
只有 BEGIN/END 之间的这一段会被注入
MOSS-STUDIO:AGENT-END
```

本文件自带一对**真实**托管标记，圈住下面这段示例规则——直接把本文件复制为
`AGENT.md`（或放进 `agents/`）即可生效，注入的就是标记之间的这几条；本段说明与
上面的示意都不会注入。想自定义就改标记之间的内容，或删掉这对标记改用整文件注入：

<!-- MOSS-STUDIO:AGENT:BEGIN -->
- Terminology: 奥特曼→Ultraman, 怪兽→kaiju, 特摄→tokusatsu.
- Style: concise, concrete, visually descriptive English; no flowery adjectives.
- 仅 Editor 的 AI 拆解时：优先合并相邻元素，不超过 5 个框；翻译与反推时忽略本条（条目结构必须与源逐字一致）。
<!-- MOSS-STUDIO:AGENT:END -->
