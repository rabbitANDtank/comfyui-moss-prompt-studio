/**
 * 翻译器节点的 LLM 配置栏与 agent_md 空值纠正（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * initTranslator：MOSSTranslator 挂一条 ⇄同步配置栏（把连接配置复制到另外两个 MOSS
 * 节点，不含 api_key）；sanitizeAgentMd（Editor/Translator 共用，由 editor_canvas
 * re-import）修旧工作流 agent_md 下拉存出的空值。
 */
import { app } from "/scripts/app.js";
import { normalizeIntWidget } from "./model.js";
import { syncLlmConfig, warnApiKeyIfSet, bindApiKeyWarn } from "./llm_config.js";
import { injectStyle } from "./styles.js";

const AGENT_MD_DEFAULT = "(默认 AGENT.md)";

// agent_md 下拉的空值纠正：自动恢复的旧工作流可能存出 ''，后端虽然兼容，
// 但界面统一显示成默认项，避免下拉框出现空值引发困惑
export function sanitizeAgentMd(node) {
  try {
    const w = node.widgets && node.widgets.find((x) => x.name === "agent_md");
    if (w && !String(w.value || "").trim()) w.value = AGENT_MD_DEFAULT;
  } catch (e) { /* noop */ }
}

// 翻译器节点：挂一条 LLM 配置栏（⇄同步到另外两个 MOSS 节点）
export function initTranslator(node) {
  if (node._mpsCfgInit) return;
  node._mpsCfgInit = true;
  injectStyle();
  if (!(node.widgets || []).some(w => w.name === "provider")) return;
  if (typeof node.addDOMWidget !== "function") return;
  sanitizeAgentMd(node);
  // ≤v1.7.2 工作流 widgets_values 错位会把 canvas_width 读成 ""（INT 收字符串），
  // 服务端校验失败时采样输出分支被静默忽略——“运行成功但不出图”。这里静默修回。
  const normalizeCanvasWidgets = (n) => {
    for (const nm of ["canvas_width", "canvas_height"]) {
      const w = (n.widgets || []).find((x) => x.name === nm);
      if (!w) continue;
      const v = normalizeIntWidget(w.value);
      if (w.value !== v) {
        w.value = v;
        if (w.inputEl) w.inputEl.value = v;
      }
    }
  };
  normalizeCanvasWidgets(node);
  const prevCfg = node.onConfigure;
  node.onConfigure = function (...a) {
    const r = prevCfg ? prevCfg.apply(this, a) : undefined;
    sanitizeAgentMd(this);
    normalizeCanvasWidgets(this);
    return r;
  };

  const bar = document.createElement("div");
  bar.className = "mps-cfgbar";
  const msg = document.createElement("span");
  msg.className = "mps-cfgmsg";
  const notify = (t) => {
    msg.textContent = t;
    setTimeout(() => { if (msg.textContent === t) msg.textContent = ""; }, 2500);
  };
  const btn = document.createElement("button");
  btn.textContent = "⇄同步";
  btn.title = "把当前 LLM 连接配置（不含 api_key）复制到另外两个 MOSS 节点；key 建议放 config.json";
  btn.addEventListener("click", () => {
    const n = syncLlmConfig(node, app.graph, app);
    notify(`已同步连接配置（不含 api_key）到另外 ${n} 个节点`);
  });
  bar.append(btn, msg);

  // 明文 key 一次性警告（工作流/执行历史会保存 widget 的 api_key；同 key 只提示一次）
  const showKeyWarn = (t) => {
    try {
      app.extensionManager?.toast?.add({ severity: "warn", summary: "MOSS Studio", detail: t, life: 6000 });
    } catch (e) { /* toast 可选 */ }
    notify("⚠ " + t);
  };
  warnApiKeyIfSet(node, showKeyWarn);
  bindApiKeyWarn(node, showKeyWarn);

  const domw = node.addDOMWidget("LLM配置", "mps_cfg", bar, { serialize: false, hideOnZoom: false });
  domw.computeSize = function (width) {
    return [Math.max(Number(width) || 300, 300), 44];
  };
  bar.addEventListener("pointerdown", (e) => e.stopPropagation());
}
