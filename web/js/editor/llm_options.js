/**
 * Editor 的 LLM / AI 选项双列面板（DOM widget）。
 *
 * 标准 widget 仍然保留在 node.widgets 中参与工作流序列化，只是被隐藏；
 * 这里用原生 DOM 控件替代显示，并把值写回标准 widget。这样：
 *   - 旧工作流 widgets_values 顺序/数量不变；
 *   - 后端 INPUT_TYPES / API 提交完全不变；
 *   - 节点上只占一个约两列高的面板，而不是十条单列 widget。
 *
 * 纯逻辑导出（widgetKind / normalizeComboOptions / parseNumberInput）给 Node 测试；
 * mountLlmOptions 只在浏览器环境调用。
 */

export const LLM_OPTION_LEFT = Object.freeze([
  "ai_draft", "provider", "base_url", "model", "api_key",
]);

export const LLM_OPTION_RIGHT = Object.freeze([
  "temperature", "thinking", "timeout", "agent_md",
]);

// ai_prompt 是多行文本，横跨两列显示；仍然属于同一配置区。
export const LLM_OPTION_FULL = Object.freeze(["ai_prompt"]);

export const LLM_OPTION_NAMES = Object.freeze([
  ...LLM_OPTION_LEFT, ...LLM_OPTION_RIGHT, ...LLM_OPTION_FULL,
]);

export const LLM_OPTION_SPEC = Object.freeze({
  ai_draft: {
    title: "开启后，散文输入会调用 LLM 拆解为布局草稿；已是 layout/已编译时直接透传",
  },
  provider: {
    title: "LLM 提供方（手动切换，无自动 failover）；custom 可填任意 OpenAI 兼容地址",
  },
  base_url: {
    title: "留空则读取 config.json 或 provider 预设地址",
    placeholder: "留空=config.json/预设",
  },
  model: {
    title: "留空则读取 config.json 或 provider 预设模型",
    placeholder: "留空=预设",
  },
  api_key: {
    title: "EMPTY 表示读取 config.json；不建议把明文 key 保存在工作流里",
    placeholder: "EMPTY=config.json",
  },
  temperature: {
    title: "采样温度（0–2，步进 0.05）",
    min: 0, max: 2, step: 0.05,
  },
  thinking: {
    title: "思考档位：off / low / medium / high；deepseek 会映射到 reasoning_effort",
  },
  timeout: {
    title: "请求超时（秒，10–1200）",
    min: 10, max: 1200, step: 1,
  },
  agent_md: {
    title: "追加注入的 AGENT.md；默认读取插件根目录，(不注入) 可完全关闭",
  },
  ai_prompt: {
    title: "⚡拆解使用的 system prompt；留空使用内建默认",
    placeholder: "留空使用内建提示词（拆解 system prompt）",
  },
});

/** 输入槽是否由连线提供值（连线优先：面板控件只读，值来自上游）。 */
export function inputConnected(inputs, name) {
  const slot = (Array.isArray(inputs) ? inputs : []).find(
    (i) => i && ((i.widget && i.widget.name === name) || i.name === name),
  );
  return !!(slot && slot.link != null);
}

/** 把 ComfyUI widget 类型归成面板需要的几类控件。 */
export function widgetKind(widget) {
  const t = widget && widget.type;
  if (t === "toggle" || t === "boolean") return "toggle";
  if (t === "combo") return "combo";
  if (t === "number" || t === "int" || t === "float") return "number";
  if (t === "customtext" || t === "textarea") return "textarea";
  return "text";
}

/** 过滤空值/重复项，并保证当前值在选项里；不修改传入数组。 */
export function normalizeComboOptions(values, current) {
  const out = [];
  for (const value of Array.isArray(values) ? values : []) {
    const text = String(value ?? "");
    if (!text || out.includes(text)) continue;
    out.push(text);
  }
  const cur = String(current ?? "");
  if (cur && !out.includes(cur)) out.push(cur);
  return out;
}

/** 数字输入解析：空/非法返回 null；合法则按 min/max 夹取。 */
export function parseNumberInput(value, spec = {}) {
  if (value === "" || value == null) return null;
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  let out = n;
  if (Number.isFinite(spec.min)) out = Math.max(out, spec.min);
  if (Number.isFinite(spec.max)) out = Math.min(out, spec.max);
  return Math.round(out * 1000) / 1000;
}

const STYLE_ID = "mps-llmopts-style";

const CSS = `
.mps-llmopts{box-sizing:border-box;width:100%;padding:6px;background:#1b1b1b;border:1px solid #3a3a3a;border-radius:6px;font-family:system-ui,'Segoe UI',sans-serif;color:#ddd;font-size:12px;user-select:none;}
.mps-llmopts-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 12px;}
.mps-llmopts-col{display:flex;flex-direction:column;gap:4px;min-width:0;}
.mps-llmopts-row{display:flex;align-items:center;gap:6px;min-width:0;height:22px;}
.mps-llmopts-row>label{flex:0 0 76px;color:#a5a5a5;font-size:11px;text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
/* 字段名用 CSS content 渲染：避免浏览器整页翻译把 provider/api_key 改成中文。 */
.mps-llmopts-row>label::before,.mps-llmopts-full>label::before{content:attr(data-label);}
.mps-llmopts-row>input[type=text],.mps-llmopts-row>input[type=password],.mps-llmopts-row>input[type=number],.mps-llmopts-row>select{flex:1 1 auto;min-width:0;height:22px;box-sizing:border-box;background:#262626;color:#eee;border:1px solid #444;border-radius:4px;padding:1px 5px;font-size:12px;}
.mps-llmopts-row>input[type=checkbox]{flex:0 0 auto;width:14px;height:14px;margin:0;accent-color:#4a7bd0;}
.mps-llmopts-row.connected>input,.mps-llmopts-row.connected>select{opacity:.55;}
.mps-llmopts-full{display:flex;flex-direction:column;gap:2px;margin-top:4px;}
.mps-llmopts-full>label{color:#a5a5a5;font-size:11px;}
.mps-llmopts-full>textarea{width:100%;box-sizing:border-box;min-height:52px;background:#262626;color:#eee;border:1px solid #444;border-radius:4px;padding:4px;font:12px/1.35 system-ui,'Segoe UI',sans-serif;resize:vertical;}
.mps-llmopts-full.connected>textarea{opacity:.55;}
.mps-llmopts input:disabled,.mps-llmopts select:disabled,.mps-llmopts textarea:disabled{cursor:not-allowed;}
`;

function ensureStyle() {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}

/**
 * 在 Editor 节点上挂载双列选项面板。
 * 返回 { refresh, dispose, panel }；重复调用返回同一个实例。
 */
export function mountLlmOptions(node) {
  if (!node || node._mpsLlmOptions) return node ? node._mpsLlmOptions || null : null;
  if (typeof document === "undefined" || typeof node.addDOMWidget !== "function") return null;

  // 每次按名字从当前 node.widgets 取：新版前端在 configure 时可能替换 widget 对象，
  // 不能捕获 mount 时的数组/对象，否则后续编辑会写到旧 widget。
  const get = (name) => (node.widgets || []).find((w) => w.name === name);
  // 只处理具备这些核心字段的 Editor；字段缺失时降级跳过，避免影响旧节点。
  if (!get("ai_draft") || !get("provider") || !get("agent_md")) return null;

  ensureStyle();

  const panel = document.createElement("div");
  panel.className = "mps-llmopts notranslate";
  panel.setAttribute("translate", "no");
  const grid = document.createElement("div");
  grid.className = "mps-llmopts-grid";
  const leftCol = document.createElement("div");
  leftCol.className = "mps-llmopts-col";
  const rightCol = document.createElement("div");
  rightCol.className = "mps-llmopts-col";
  grid.append(leftCol, rightCol);
  panel.append(grid);

  const controls = new Map();
  const baseTitles = new Map();

  const isConnected = (name) => inputConnected(node.inputs, name);

  const rowOf = (el) => el.closest(".mps-llmopts-row") || el.closest(".mps-llmopts-full");

  const notifyWidget = (name, w, old) => {
    try { w.callback?.call(w, w.value, globalThis.app?.canvas, node, [0, 0], null); } catch (e) { /* noop */ }
    try { node.onWidgetChanged?.(name, w.value, old, w); } catch (e) { /* noop */ }
  };

  const apply = (name, value, { silent = false } = {}) => {
    const w = get(name);
    if (!w || isConnected(name)) return false;
    let next = value;
    if (widgetKind(w) === "toggle") next = !!value;
    const old = w.value;
    if (Object.is(old, next)) return false;
    w.value = next;
    if (!silent) notifyWidget(name, w, old);
    try { node.setDirtyCanvas?.(true, true); } catch (e) { /* noop */ }
    return true;
  };

  const refresh = () => {
    if (panel.contains(document.activeElement)) return;
    for (const [name, el] of controls) {
      const w = get(name);
      if (!w) continue;
      const kind = widgetKind(w);
      const connected = isConnected(name);
      el.disabled = connected;
      const row = rowOf(el);
      if (row) row.classList.toggle("connected", connected);
      const base = baseTitles.get(name) || "";
      el.title = connected ? `${base}（当前由连线提供；断开连线后可在此编辑）` : base;
      if (kind === "toggle") el.checked = !!w.value;
      else if (kind === "combo") {
        const text = String(w.value ?? "");
        if (text && !Array.from(el.options).some((o) => o.value === text)) {
          el.append(new Option(text, text));
        }
        el.value = text;
      } else {
        el.value = String(w.value ?? "");
      }
    }
  };

  const createControl = (name) => {
    const w = get(name);
    const spec = LLM_OPTION_SPEC[name] || {};
    const kind = widgetKind(w);
    let el;
    if (kind === "toggle") {
      el = document.createElement("input");
      el.type = "checkbox";
      el.checked = !!w.value;
    } else if (kind === "combo") {
      el = document.createElement("select");
      const values = normalizeComboOptions(w.options?.values || w.values, w.value);
      for (const value of values) el.append(new Option(value, value));
      el.value = String(w.value ?? "");
    } else if (kind === "number") {
      el = document.createElement("input");
      el.type = "number";
      if (Number.isFinite(spec.min)) el.min = String(spec.min);
      if (Number.isFinite(spec.max)) el.max = String(spec.max);
      if (Number.isFinite(spec.step)) el.step = String(spec.step);
      el.value = String(w.value ?? "");
    } else if (kind === "textarea") {
      el = document.createElement("textarea");
      el.rows = 3;
      if (spec.placeholder) el.placeholder = spec.placeholder;
      el.value = String(w.value ?? "");
    } else {
      el = document.createElement("input");
      el.type = "text";
      if (spec.placeholder) el.placeholder = spec.placeholder;
      el.value = String(w.value ?? "");
    }
    el.dataset.mpsLlmName = name;
    const title = spec.title || "";
    baseTitles.set(name, title);
    el.title = title;
    return el;
  };

  const bindControl = (name, el) => {
    const kind = widgetKind(get(name));
    if (kind === "toggle" || kind === "combo") {
      el.addEventListener("change", () => apply(name, kind === "toggle" ? el.checked : el.value));
      return;
    }
    if (kind === "number") {
      el.addEventListener("input", () => {
        const n = parseNumberInput(el.value, LLM_OPTION_SPEC[name] || {});
        if (n != null) apply(name, n);
      });
      el.addEventListener("blur", () => {
        const n = parseNumberInput(el.value, LLM_OPTION_SPEC[name] || {});
        if (n != null) apply(name, n);
        refresh();
      });
      return;
    }
    // api_key：input 立即写 widget（静默），change（失焦/回车）再触发明文警告回调，
    // 避免每敲一个字符就弹一次警告，也避免 change 事件丢失导致值没写回。
    if (name === "api_key") {
      el.addEventListener("input", () => apply(name, el.value, { silent: true }));
      el.addEventListener("change", () => {
        const changed = apply(name, el.value);
        // input 已静默写入时值没变，apply 会提前返回；这里仍要触发回调，
        // 让 bindApiKeyWarn 的明文 key 警告在失焦/回车时生效。
        if (!changed && !isConnected(name)) {
          const w = get(name);
          if (w) notifyWidget(name, w, w.value);
        }
      });
      return;
    }
    el.addEventListener("input", () => apply(name, el.value));
  };

  const addRow = (host, name) => {
    if (!get(name)) return;
    const spec = LLM_OPTION_SPEC[name] || {};
    const row = document.createElement("div");
    row.className = "mps-llmopts-row";
    row.dataset.name = name;
    const label = document.createElement("label");
    label.dataset.label = spec.label || name;
    label.textContent = "";
    label.setAttribute("aria-label", label.dataset.label);
    label.title = spec.title || "";
    label.setAttribute("translate", "no");
    label.classList.add("notranslate");
    const el = createControl(name);
    label.htmlFor = (el.id = `mps-llmopts-${node.id}-${name}`);
    bindControl(name, el);
    controls.set(name, el);
    row.append(label, el);
    host.append(row);
  };

  for (const name of LLM_OPTION_LEFT) addRow(leftCol, name);
  for (const name of LLM_OPTION_RIGHT) addRow(rightCol, name);

  for (const name of LLM_OPTION_FULL) {
    if (!get(name)) continue;
    const spec = LLM_OPTION_SPEC[name] || {};
    const box = document.createElement("div");
    box.className = "mps-llmopts-full";
    box.dataset.name = name;
    const label = document.createElement("label");
    label.dataset.label = spec.label || name;
    label.textContent = "";
    label.setAttribute("aria-label", label.dataset.label);
    label.title = spec.title || "";
    label.setAttribute("translate", "no");
    label.classList.add("notranslate");
    const el = createControl(name);
    label.htmlFor = (el.id = `mps-llmopts-${node.id}-${name}`);
    bindControl(name, el);
    controls.set(name, el);
    box.append(label, el);
    panel.append(box);
  }

  // 隔离画布拖拽/缩放/快捷键，避免在输入框里按 Delete 删除节点。
  for (const type of ["pointerdown", "wheel"]) {
    panel.addEventListener(type, (e) => e.stopPropagation(), { passive: type === "wheel" });
  }
  panel.addEventListener("keydown", (e) => e.stopPropagation());

  // 原生 resize 手柄拖动后，让节点重新分配 DOM widget 高度。
  let resizeTimer = 0;
  let resizeObserver = null;
  const aiPromptEl = controls.get("ai_prompt");
  if (aiPromptEl && typeof ResizeObserver === "function") {
    resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        try { node.arrange?.(); } catch (e) { /* noop */ }
        try { node.setDirtyCanvas?.(true, true); } catch (e) { /* noop */ }
      }, 80);
    });
    resizeObserver.observe(aiPromptEl);
  }

  // 隐藏原标准 widget：值仍参与序列化，只是不再占画布行。
  const hideWidgets = () => {
    for (const name of LLM_OPTION_NAMES) {
      const w = get(name);
      if (!w) continue;
      if (w.hidden) continue; // 已隐藏就不重复写，避免每秒触发一次重排
      try { w.hidden = true; } catch (e) {
        try { w.visibility.suppression.byExtension = true; } catch (e2) { /* noop */ }
      }
    }
  };
  hideWidgets();

  const dw = node.addDOMWidget("LLM 选项", "mps_llmopts", panel, {
    serialize: false,
    hideOnZoom: false,
  });
  dw.serialize = false;
  if (dw.options) dw.options.serialize = false;

  // 插到 ai_draft 的位置：在 source/canvas_width/canvas_height/edits 之后、
  // 其余 pose 控件之前；DOM widget 不序列化，不会改变 widgets_values 顺序。
  const firstCfg = get("ai_draft");
  const insertAt = firstCfg ? node.widgets.indexOf(firstCfg) : -1;
  if (insertAt >= 0) {
    const own = node.widgets.indexOf(dw);
    if (own >= 0) node.widgets.splice(own, 1);
    node.widgets.splice(insertAt, 0, dw);
  }

  dw.computeSize = (width) => [
    Math.max(Number(width) || 320, 320),
    // 用 scrollHeight 而不是 offsetHeight：首次布局时 offsetHeight 可能还是被挤压后的值。
    Math.max(224, Math.ceil((panel.scrollHeight || panel.offsetHeight || 224) + 8)),
  ];

  const timer = setInterval(() => {
    try {
      // 前端在 configure/重建时可能替换 widget 对象；新对象默认可见，这里兜底重新隐藏。
      hideWidgets();
      refresh();
    } catch (e) { /* noop */ }
  }, 1000);
  const prevRemoved = node.onRemoved;
  node.onRemoved = function (...args) {
    clearInterval(timer);
    clearTimeout(resizeTimer);
    try { resizeObserver?.disconnect(); } catch (e) { /* noop */ }
    return prevRemoved ? prevRemoved.apply(this, args) : undefined;
  };

  const raf = typeof requestAnimationFrame === "function"
    ? requestAnimationFrame
    : (fn) => setTimeout(fn, 0);
  raf(() => {
    try { node.arrange?.(); } catch (e) { /* noop */ }
    try { node.setDirtyCanvas?.(true, true); } catch (e) { /* noop */ }
  });
  refresh();

  const instance = {
    panel,
    domWidget: dw,
    controls,
    refresh,
    hideWidgets,
    dispose: () => {
      clearInterval(timer);
      clearTimeout(resizeTimer);
      try { resizeObserver?.disconnect(); } catch (e) { /* noop */ }
      const own = node.widgets.indexOf(dw);
      if (own >= 0) node.widgets.splice(own, 1);
      try { panel.remove(); } catch (e) { /* noop */ }
      for (const name of LLM_OPTION_NAMES) {
        const w = get(name);
        if (w) { try { w.hidden = false; } catch (e) { /* noop */ } }
      }
      try { node.setDirtyCanvas?.(true, true); } catch (e) { /* noop */ }
    },
  };
  node._mpsLlmOptions = instance;
  return instance;
}
