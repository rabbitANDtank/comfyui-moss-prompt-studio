/**
 * Editor 的参考输入双列面板（pose_in / pose_in_2、costume_in / costume_in_2）。
 *
 * 与 llm_options.js 同样的思路：标准 widget 保留并参与序列化，只隐藏显示；
 * 这里用两个并排的 textarea 代替，右下角保留原生 resize 手柄。
 */

export const POSE_INPUT_FIELDS = Object.freeze(["pose_in", "pose_in_2"]);
export const COSTUME_INPUT_FIELDS = Object.freeze(["costume_in", "costume_in_2"]);

/**
 * 参考输入单元格的显示/可编辑状态（纯逻辑）：连线优先 —— 有连线时控件禁用，
 * 并在标题里说明「断开连线后可编辑」。mountReferenceInputs 的 refresh 直接使用。
 */
export function referenceInputDisplay({ connected = false, value = "", title = "" } = {}) {
  const linked = !!connected;
  const suffix = "（当前由连线提供；断开连线后可在此编辑）";
  return {
    connected: linked,
    disabled: linked,
    value: String(value ?? ""),
    title: linked ? `${String(title || "")}${suffix}` : String(title || ""),
  };
}

/** 兼容旧命名（姿势面板测试与调用方）。 */
export function poseInputDisplay(opts) {
  return referenceInputDisplay(opts);
}

export const POSE_INPUT_SPEC = Object.freeze({
  pose_in: {
    placeholder: "姿势参考①：点工具栏 🎯姿势① 选择，或粘贴/连线任意动作描述",
    title: "姿势参考①：点 🎯姿势① 从图库选，也可手填或连线 STRING；连线值优先。仅 ⚡拆解/📥注入时生效",
  },
  pose_in_2: {
    placeholder: "姿势参考②：双人分别指定时给另一角色，点 🎯姿势② 选择",
    title: "姿势参考②：双人分别指定姿势时给另一角色；点 🎯姿势② 选择或手填/连线",
  },
});

export const COSTUME_INPUT_SPEC = Object.freeze({
  costume_in: {
    placeholder: "服装参考①：连接 Costume Gallery 的 description，或粘贴任意服装描述",
    title: "服装参考①：把 comfyui-costume-gallery 的 description（分类 / 关键词…）连到这里，或手填/连线服装描述；连线值优先。仅 ⚡拆解/📥服装时生效",
  },
  costume_in_2: {
    placeholder: "服装参考②：双人分别指定时给另一角色",
    title: "服装参考②：双人分别指定服装时给另一角色（与 costume_in 配对）；手填或连接第二个 Costume Gallery 的 description",
  },
});

const STYLE_ID = "mps-poseinputs-style";

const CSS = `
.mps-poseinputs{box-sizing:border-box;width:100%;padding:6px;background:#1b1b1b;border:1px solid #3a3a3a;border-radius:6px;font-family:system-ui,'Segoe UI',sans-serif;color:#ddd;font-size:12px;user-select:none;}
.mps-poseinputs-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}
.mps-poseinputs-cell{display:flex;flex-direction:column;gap:2px;min-width:0;}
.mps-poseinputs-cell>label{color:#a5a5a5;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mps-poseinputs-cell>label::before{content:attr(data-label);}
.mps-poseinputs-cell>textarea{width:100%;box-sizing:border-box;min-height:54px;background:#262626;color:#eee;border:1px solid #444;border-radius:4px;padding:4px;font:12px/1.35 system-ui,'Segoe UI',sans-serif;resize:vertical;}
.mps-poseinputs-cell.connected>textarea{opacity:.55;}
.mps-poseinputs textarea:disabled{cursor:not-allowed;}
`;

function ensureStyle() {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}

const POSE_CONFIG = {
  fields: POSE_INPUT_FIELDS,
  specs: POSE_INPUT_SPEC,
  title: "姿势参考",
  domName: "mps_poseinputs",
  storeKey: "_mpsPoseInputs",
  idPrefix: "mps-pose",
  panelClass: "mps-poseinputs",
};

const COSTUME_CONFIG = {
  fields: COSTUME_INPUT_FIELDS,
  specs: COSTUME_INPUT_SPEC,
  title: "服装参考",
  domName: "mps_costumeinputs",
  storeKey: "_mpsCostumeInputs",
  idPrefix: "mps-costume",
  panelClass: "mps-poseinputs mps-costumeinputs",
};

/**
 * 在 Editor 节点上挂载双列参考面板。
 * 返回 { refresh, dispose, panel }；重复调用返回同一个实例。
 */
export function mountReferenceInputs(node, cfg) {
  if (!node || node[cfg.storeKey]) return node ? node[cfg.storeKey] || null : null;
  if (typeof document === "undefined" || typeof node.addDOMWidget !== "function") return null;

  // 动态取：前端 configure 时可能替换 widget 对象。
  const get = (name) => (node.widgets || []).find((w) => w.name === name);
  if (cfg.fields.some((name) => !get(name))) return null;

  ensureStyle();

  const panel = document.createElement("div");
  panel.className = cfg.panelClass + " notranslate";
  panel.setAttribute("translate", "no");
  const grid = document.createElement("div");
  grid.className = "mps-poseinputs-grid";
  panel.append(grid);

  const controls = new Map();

  const isConnected = (name) => {
    const slot = (node.inputs || []).find(
      (i) => i && ((i.widget && i.widget.name === name) || i.name === name),
    );
    return !!(slot && slot.link != null);
  };

  const apply = (name, value) => {
    const w = get(name);
    if (!w || isConnected(name)) return false;
    const old = w.value;
    if (Object.is(old, value)) return false;
    w.value = value;
    try { w.callback?.call(w, value, globalThis.app?.canvas, node, [0, 0], null); } catch (e) { /* noop */ }
    try { node.onWidgetChanged?.(name, value, old, w); } catch (e) { /* noop */ }
    try { node.setDirtyCanvas?.(true, true); } catch (e) { /* noop */ }
    return true;
  };

  const refresh = () => {
    if (panel.contains(document.activeElement)) return;
    for (const [name, el] of controls) {
      const w = get(name);
      if (!w) continue;
      const spec = cfg.specs[name] || {};
      const state = referenceInputDisplay({
        connected: isConnected(name), value: w.value, title: spec.title || "",
      });
      el.disabled = state.disabled;
      const cell = el.closest(".mps-poseinputs-cell");
      if (cell) cell.classList.toggle("connected", state.connected);
      el.title = state.title;
      el.value = state.value;
    }
  };

  const fields = [];
  for (const name of cfg.fields) {
    const w = get(name);
    if (!w) continue;
    fields.push(name);
    const spec = cfg.specs[name] || {};
    const cell = document.createElement("div");
    cell.className = "mps-poseinputs-cell";
    cell.dataset.name = name;
    const label = document.createElement("label");
    label.dataset.label = name;
    label.textContent = "";
    label.setAttribute("aria-label", name);
    label.title = spec.title || "";
    label.setAttribute("translate", "no");
    label.classList.add("notranslate");
    const el = document.createElement("textarea");
    el.rows = 3;
    el.placeholder = spec.placeholder || "";
    el.value = String(w.value ?? "");
    el.title = spec.title || "";
    el.dataset.mpsInputName = name;
    label.htmlFor = (el.id = `${cfg.idPrefix}-${node.id}-${name}`);
    el.addEventListener("input", () => apply(name, el.value));
    controls.set(name, el);
    cell.append(label, el);
    grid.append(cell);
  }
  if (!fields.length) return null;

  // 隔离画布拖拽/缩放/快捷键。
  for (const type of ["pointerdown", "wheel"]) {
    panel.addEventListener(type, (e) => e.stopPropagation(), { passive: type === "wheel" });
  }
  panel.addEventListener("keydown", (e) => e.stopPropagation());

  const hideWidgets = () => {
    for (const name of fields) {
      const w = get(name);
      if (!w || w.hidden) continue;
      try { w.hidden = true; } catch (e) {
        try { w.visibility.suppression.byExtension = true; } catch (e2) { /* noop */ }
      }
    }
  };
  hideWidgets();

  const dw = node.addDOMWidget(cfg.title, cfg.domName, panel, {
    serialize: false,
    hideOnZoom: false,
  });
  dw.serialize = false;
  if (dw.options) dw.options.serialize = false;

  // 插到第一个参考 widget 的位置：上一组面板/开关在其前，画布 DOM widget 在其后。
  const firstField = get(cfg.fields[0]);
  const insertAt = firstField ? node.widgets.indexOf(firstField) : -1;
  if (insertAt >= 0) {
    const own = node.widgets.indexOf(dw);
    if (own >= 0) node.widgets.splice(own, 1);
    node.widgets.splice(insertAt, 0, dw);
  }

  dw.computeSize = (width) => [
    Math.max(Number(width) || 320, 320),
    Math.max(96, Math.ceil((panel.scrollHeight || panel.offsetHeight || 96) + 8)),
  ];

  // 原生 resize 手柄拖动后，让节点重新分配 DOM widget 高度。
  let resizeTimer = 0;
  let resizeObserver = null;
  if (typeof ResizeObserver === "function") {
    resizeObserver = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        try { node.arrange?.(); } catch (e) { /* noop */ }
        try { node.setDirtyCanvas?.(true, true); } catch (e) { /* noop */ }
      }, 80);
    });
    for (const el of controls.values()) resizeObserver.observe(el);
  }

  const timer = setInterval(() => {
    try {
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
      for (const name of fields) {
        const w = get(name);
        if (w) { try { w.hidden = false; } catch (e) { /* noop */ } }
      }
      try { node.setDirtyCanvas?.(true, true); } catch (e) { /* noop */ }
    },
  };
  node[cfg.storeKey] = instance;
  return instance;
}

export function mountPoseInputs(node) {
  return mountReferenceInputs(node, POSE_CONFIG);
}

export function mountCostumeInputs(node) {
  return mountReferenceInputs(node, COSTUME_CONFIG);
}
