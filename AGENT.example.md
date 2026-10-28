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
- 默认**整个文件**都会被注入（删掉本说明，写你自己的内容即可）
- 若文件里还想要"不注入的笔记"，用成对标记圈定要注入的片段（各恰好一次），例如：

```
这里是我的笔记，不会被注入
<!-- MOSS-STUDIO:AGENT:BEGIN -->
只有 BEGIN/END 之间的这一段会被注入
<!-- MOSS-STUDIO:AGENT:END -->
```

下面是可直接使用的示例内容（复制成 AGENT.md 后按需修改）：

- Terminology: 奥特曼→Ultraman, 怪兽→kaiju, 特摄→tokusatsu.
- Style: concise, concrete, visually descriptive English; no flowery adjectives.
- 拆解时优先合并相邻元素，不超过 5 个框。
