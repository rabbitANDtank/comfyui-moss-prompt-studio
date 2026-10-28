/**
 * ComfyUI 生命周期适配层
 *
 * 负责 Queue / 快捷键 flush、节点生命周期钩子的链式挂载。
 * 不包含画布渲染、拖拽几何或业务校验；这些在 model.js / editor_canvas.js。
 */

export function createEditorFlushRegistry() {
  const subs = new Set();
  let wrappedQueue = null;
  let shortcutInstalled = false;

  const flushAll = () => {
    for (const fn of subs) {
      try { fn(); } catch (e) { /* noop */ }
    }
  };

  const register = (fn) => {
    subs.add(fn);
    return () => subs.delete(fn);
  };

  /**
   * 安装 Queue 前 flush 与 Ctrl/Cmd+Enter flush。
   * 可重复调用；同一个 app.queuePrompt 只包一次。
   */
  const install = (app) => {
    if (app && typeof app.queuePrompt === "function" && app.queuePrompt !== wrappedQueue) {
      const orig = app.queuePrompt;
      wrappedQueue = function (...args) {
        flushAll();
        return orig.apply(this, args);
      };
      app.queuePrompt = wrappedQueue;
    }
    if (!shortcutInstalled && typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") flushAll();
      }, true);
      shortcutInstalled = true;
    }
  };

  return { register, flushAll, install, get size() { return subs.size; } };
}

export const editorFlushRegistry = createEditorFlushRegistry();

/**
 * 链式挂载 Editor 生命周期钩子，保留前端/其他扩展原有的 onConfigure/onResize/onRemoved。
 * callbacks 里的 this 指向当前 node。
 */
export function mountEditorLifecycle(node, callbacks = {}) {
  const { onConfigure, onResize, onRemoved } = callbacks;

  const prevConfigure = node.onConfigure;
  node.onConfigure = function (...args) {
    const result = prevConfigure ? prevConfigure.apply(this, args) : undefined;
    if (onConfigure) onConfigure.apply(this, args);
    return result;
  };

  const prevResize = node.onResize;
  node.onResize = function (...args) {
    if (onResize) onResize.apply(this, args);
    if (prevResize) return prevResize.apply(this, args);
  };

  const prevRemoved = node.onRemoved;
  node.onRemoved = function (...args) {
    if (onRemoved) onRemoved.apply(this, args);
    if (prevRemoved) return prevRemoved.apply(this, args);
  };
}


export function mountCompilerSync(node) {
  if (node._mpsSizeSync) return;
  node._mpsSizeSync = true;

  const getWidget = (n, name) => ((n && n.widgets) || []).find((w) => w.name === name);
  if (!getWidget(node, "width") || !getWidget(node, "height")) return;

  const graphOf = () => node.graph || (typeof globalThis !== "undefined" && globalThis.app && globalThis.app.graph) || null;
  const inputIndex = (name) => ((node.inputs) || []).findIndex((i) => i.name === name);
  const linkById = (lid) => {
    const g = graphOf();
    if (!g || !g.links) return null;
    const keys = [lid, String(lid)];
    if (typeof g.links.get === "function") {
      for (const k of keys) { const l = g.links.get(k); if (l) return l; }
      return null;
    }
    if (Array.isArray(g.links)) {
      return g.links.find((l) => l && (l.id === lid || String(l.id) === String(lid))) || null;
    }
    for (const k of keys) { if (g.links[k]) return g.links[k]; }
    return null;
  };
  const nodeClass = (n) => (n.constructor && n.constructor.comfyClass) || n.comfyClass || n.type;
  const nodeFromLink = (link) => {
    if (!link) return null;
    const g = graphOf();
    const raw = link.origin_id;
    return (g && (
      (g.getNodeById && (g.getNodeById(raw) || g.getNodeById(String(raw)))) ||
      (g._nodes_by_id && (g._nodes_by_id[raw] || g._nodes_by_id[String(raw)]))
    )) || null;
  };
  const readInputLink = (n, name) => {
    const idx = ((n && n.inputs) || []).findIndex((i) => i.name === name);
    if (idx < 0) return null;
    // 新版前端推荐 getInputLink（返回 Link 对象）；旧版回退到 input.link。
    if (typeof n.getInputLink === "function") {
      const l = n.getInputLink(idx);
      if (!l) return null;
      return typeof l === "object" ? l : linkById(l);
    }
    const lid = n.inputs[idx].link;
    return lid == null ? null : linkById(lid);
  };
  const linkOf = (name) => readInputLink(node, name);
  const snapEditorDim = (v) => Math.max(64, Math.min(15360, Math.round(v / 16) * 16));
  const sourceWidgetValue = (name) => {
    const src = nodeFromLink(linkOf(name));
    if (!src) return null;
    const cls = nodeClass(src);
    if (cls === "MOSSEditor") {
      // 优先读 edits 里的 layout 尺寸：比例/像素调整后它一定被重写，与画布结果同源；
      // caption 形态的 edits 没有 width/height，再回落到 canvas_width/height widget。
      const key = name === "canvas_width" ? "width" : "height";
      const edits = getWidget(src, "edits");
      if (edits && edits.value) {
        try {
          const d = JSON.parse(edits.value);
          const v = Number(d && d[key]);
          if (Number.isFinite(v)) return snapEditorDim(v);
        } catch (e) { /* 非法 JSON：回落 widget */ }
      }
      const w = getWidget(src, name);
      const v = w ? Number(w.value) : NaN;
      return Number.isFinite(v) ? snapEditorDim(v) : null;
    }
    const names = name === "canvas_width" ? ["canvas_width", "width"] : ["canvas_height", "height"];
    for (const n of names) {
      const w = getWidget(src, n);
      const v = w ? Number(w.value) : NaN;
      if (Number.isFinite(v)) return v;
    }
    return null;
  };

  // 沿 draft 上游找 Editor：直接接 Editor，或经过 Translator（可多层）都认。
  const upstreamEditor = () => {
    let src = nodeFromLink(readInputLink(node, "draft"));
    for (let depth = 0; depth < 4 && src; depth++) {
      const cls = nodeClass(src);
      if (cls === "MOSSEditor") return src;
      if (cls !== "MOSSTranslator") return null;
      src = nodeFromLink(readInputLink(src, "draft"));
    }
    return null;
  };

  // 旧工作流保存的 Compiler 没有 canvas_width/height 连线（甚至没有这两个输入槽），
  // F5 后手动连线会丢。这里从 draft 上游自动补连线；已接好则不动，每轮最多尝试一次。
  const autoWire = () => {
    if (node._mpsAutoWired) return;
    if (linkOf("canvas_width") && linkOf("canvas_height")) {
      node._mpsAutoWired = true;
      return;
    }
    const ed = upstreamEditor();
    if (!ed) return;
    const widthIdx = inputIndex("canvas_width");
    const heightIdx = inputIndex("canvas_height");
    const edWidth = ((ed.outputs) || []).findIndex((o) => o.name === "width");
    const edHeight = ((ed.outputs) || []).findIndex((o) => o.name === "height");
    let wired = false;
    try {
      if (!linkOf("canvas_width") && widthIdx >= 0 && edWidth >= 0) {
        wired = !!ed.connect(edWidth, node, widthIdx) || wired;
      }
      if (!linkOf("canvas_height") && heightIdx >= 0 && edHeight >= 0) {
        wired = !!ed.connect(edHeight, node, heightIdx) || wired;
      }
    } catch (e) { /* 下一轮再试 */ }
    if (linkOf("canvas_width") && linkOf("canvas_height")) {
      node._mpsAutoWired = true;
      if (wired) console.log("[MOSS Studio] Compiler 已自动连接 Editor 尺寸输出", node.id);
    }
  };

  let applying = false;
  const syncOnce = () => {
    if (applying) return;
    applying = true;
    let changed = false;
    try {
      autoWire();
      // 每次轮询都重新取 widget：新版前端在工作流加载/配置过程中可能替换 widget 对象。
      const pairs = [["width", "canvas_width"], ["height", "canvas_height"]];
      for (const [targetName, sourceName] of pairs) {
        const widget = getWidget(node, targetName);
        if (!widget) continue;
        const v = sourceWidgetValue(sourceName);
        if (v == null) {
          if (!node._mpsSyncWarned && linkOf(sourceName)) {
            node._mpsSyncWarned = true;
            console.warn("[MOSS Studio] Compiler 尺寸联动：已连线但读不到来源尺寸", node.id, sourceName);
          }
          continue;
        }
        if (Number(widget.value) !== v) { widget.value = v; changed = true; }
      }
      // 只更新界面显示；执行时后端本来就以连线值为准，不把工作流标记成"已修改"。
      if (changed) {
        if (!node._mpsSyncLogged) {
          node._mpsSyncLogged = true;
          console.log("[MOSS Studio] Compiler 尺寸联动已生效", node.id);
        }
        try { node.setDirtyCanvas(true, true); } catch (e) { /* noop */ }
      }
    } finally { applying = false; }
  };

  const timer = setInterval(syncOnce, 500);
  const prevConfigure = node.onConfigure;
  node.onConfigure = function (...a) {
    const r = prevConfigure ? prevConfigure.apply(this, a) : undefined;
    setTimeout(syncOnce, 0);
    return r;
  };
  const prevRemoved = node.onRemoved;
  node.onRemoved = function (...a) {
    clearInterval(timer);
    if (prevRemoved) return prevRemoved.apply(this, a);
  };
  setTimeout(syncOnce, 0);
  console.log("[MOSS Studio] Compiler 尺寸联动监听已启动", node.id);
  return { syncOnce, dispose: () => clearInterval(timer) };
}
