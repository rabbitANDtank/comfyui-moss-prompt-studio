/** MOSS 图像反推节点前端：连接测试按钮 + 把三路输出写回 Editor。 */
import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { parseReversePayload, pickReverseForm, collectPromptSubset } from "./editor/reverse.js";
import { syncLlmConfig, warnApiKeyIfSet, bindApiKeyWarn } from "./editor/llm_config.js";
import {
  DETAIL_WIDGETS, modeTemperature, isManual,
  normalizeReverseMode, sanitizeDetailValues, migrateBrokenWidgetValues,
} from "./editor/reverse_modes.js";

const NODE_CLASS = "MOSSImageReverse";
const ROUTE = "/moss_studio/test_connection";
const STYLE_ID = "mps-reverse-style";

function injectStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
.mps-rev{display:flex;flex-wrap:wrap;gap:5px;align-items:flex-start;padding:4px;width:100%;font-family:system-ui,'Segoe UI',sans-serif;font-size:12px;user-select:none;}
.mps-rev button{background:#333;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:3px 8px;cursor:pointer;font-size:12px;}
.mps-rev button:hover{background:#454545;}
.mps-rev button:disabled{opacity:.5;cursor:default;}
.mps-rev-status{flex:1 1 100%;padding:3px 6px;border-radius:4px;background:#1c1c1c;border:1px solid #3a3a3a;color:#bbb;white-space:pre-wrap;max-height:64px;overflow-y:auto;font-family:ui-monospace,Consolas,monospace;font-size:11px;}
.mps-rev-status.ok{color:#8fd48f;border-color:#3f5a3f;}
.mps-rev-status.warn{color:#e8c47a;border-color:#5f533a;}
.mps-rev-status.err{color:#ff8b8b;border-color:#6b3a3a;}
`;
  document.head.appendChild(style);
}

function isReverseNode(node) {
  const cls = (node.constructor && node.constructor.comfyClass) || node.comfyClass;
  return cls === NODE_CLASS;
}

function widgetValue(node, name) {
  const w = (node.widgets || []).find((x) => x.name === name);
  // 统一读 widget.value：inputEl.value 对 checkbox 是 "on"、对数字输入是字符串，
  // 会破坏 manual 值保存/恢复的类型判断。ComfyUI 会在 change 时同步 widget.value。
  return w ? w.value : undefined;
}

function findWidget(node, name) {
  return (node.widgets || []).find((x) => x.name === name);
}

function setWidgetValue(node, name, value) {
  const w = findWidget(node, name);
  if (!w) return;
  w.value = value;
  if (w.inputEl) {
    if ("checked" in w.inputEl) w.inputEl.checked = !!value;
    else w.inputEl.value = value;
  }
}

const ORIG_COMPUTE = new WeakMap();
function setDetailVisible(w, visible) {
  if (!w) return;
  if (!ORIG_COMPUTE.has(w)) ORIG_COMPUTE.set(w, w.computeSize);
  if (visible) {
    w.hidden = false;
    if (w.options) w.options.hidden = false;
    const orig = ORIG_COMPUTE.get(w);
    if (orig) w.computeSize = orig;
    else delete w.computeSize;
  } else {
    w.hidden = true;
    if (w.options) w.options.hidden = true;
    w.computeSize = () => [0, -4];
  }
}

function migrateWidgetLayout(node) {
  if (node._mpsLayoutMigrated) return false;
  const migrated = migrateBrokenWidgetValues(node.widgets_values);
  if (!migrated) return false;
  for (const [k, v] of Object.entries(migrated)) setWidgetValue(node, k, v);
  node._mpsLayoutMigrated = true;
  return true;
}

/** 没有接 Editor.reverse_in 时强制关闭 enabled：完整 Queue 不可能触发视觉调用。 */
function syncEnabledFromLinks(node) {
  const w = findWidget(node, "enabled");
  if (!w) return;
  // 工作流加载时 links 可能还没恢复：outputs 里有 link id 但 graph.links 查不到，
  // 此时不要急着关掉用户保存的 enabled，交给下一次轮询。
  const graphLinks = app.graph && app.graph.links;
  if (!graphLinks) return;
  const ids = (node.outputs || []).flatMap((o) => o.links || []);
  if (ids.length && !ids.some((id) => (graphLinks.get ? graphLinks.get(id) : graphLinks[id]))) {
    return;
  }
  let hasTarget = false;
  try {
    hasTarget = findTargets(node).some((t) => t.inputName === "reverse_in");
  } catch (e) { /* 图未就绪时保持现状 */ }
  if (!hasTarget && w.value === true) setWidgetValue(node, "enabled", false);
}

function applyModeVisibility(node) {
  migrateWidgetLayout(node);
  const modeW = findWidget(node, "reverse_mode");
  if (!modeW) return; // 旧后端没有模式控件时不隐藏任何参数
  const mode = normalizeReverseMode(modeW.value);
  if (modeW.value !== mode) {
    // 旧工作流把旧 max_items 的数字（如 6）读进了 reverse_mode，必须纠正，
    // 否则 Queue 时前端/后端都会报“值不在列表内”。
    modeW.value = mode;
    if (modeW.inputEl) modeW.inputEl.value = mode;
  }
  const manual = isManual(mode);
  const prevMode = node._mpsPrevMode;
  if (prevMode === "manual" && !manual) {
    // 离开 manual 前保存用户手调值；非 manual 期间不再写这些控件，
    // 避免模式切换静默清掉 manual 参数。
    const saved = {};
    for (const name of DETAIL_WIDGETS) saved[name] = widgetValue(node, name);
    node._mpsManualValues = saved;
  }
  if (manual && prevMode !== "manual") {
    const src = node._mpsManualValues;
    const vals = sanitizeDetailValues(
      src && typeof src === "object" ? src : Object.fromEntries(
        DETAIL_WIDGETS.map((name) => [name, widgetValue(node, name)])
      )
    );
    for (const [k, v] of Object.entries(vals)) setWidgetValue(node, k, v);
  }
  node._mpsPrevMode = mode;
  node._mpsModeApplied = mode;
  for (const name of DETAIL_WIDGETS) setDetailVisible(findWidget(node, name), manual);
  // 只在需要时增高（manual 控件需要空间）；不强制 setSize，保留用户手动尺寸。
  try {
    const needed = node.computeSize?.();
    if (needed && needed[1] > (node.size?.[1] || 0)) {
      node.setSize?.([node.size[0], needed[1]]);
    }
  } catch (e) { /* noop */ }
  node.setDirtyCanvas?.(true, true);
}

/** configure 可能替换 widget 对象；每次轮询都补一次回调包装（不重复包同一对象）。 */
function bindModeCallbacks(node) {
  for (const name of ["reverse_mode", "provider", "enabled"]) {
    const w = findWidget(node, name);
    if (!w || w._mpsModeBound) continue;
    const prev = w.callback;
    w.callback = function (...args) {
      const r = prev ? prev.apply(this, args) : undefined;
      // 勾选启用时立刻校验连接：没接 Editor.reverse_in 就马上关回去，
      // 不给“勾上后马上 Queue”留下调用 LLM 的窗口。
      if (name === "enabled") syncEnabledFromLinks(node);
      else applyModeVisibility(node);
      return r;
    };
    w._mpsModeBound = true;
  }
}

function setReverseStatus(node, text, kind) {
  const el = node && node._mpsReverseStatusEl;
  if (!el) return;
  el.className = "mps-rev-status " + (kind || "");
  el.textContent = text;
}

function findTargets(node) {
  const out = [];
  const links = app.graph && app.graph.links;
  (node.outputs || []).forEach((o, slot) => {
    for (const id of (o.links || [])) {
      const link = links && (links.get ? links.get(id) : links[id]);
      if (!link) continue;
      const target = app.graph.getNodeById ? app.graph.getNodeById(link.target_id) : null;
      if (!target) continue;
      const cls = (target.constructor && target.constructor.comfyClass) || target.comfyClass;
      if (cls !== "MOSSEditor") continue;
      const input = target.inputs && target.inputs[link.target_slot];
      out.push({ slot, target, inputName: input && input.name });
    }
  });
  return out;
}

function initNode(node) {
  if (node._mpsReverseInit) return;
  node._mpsReverseInit = true;
  injectStyle();
  if (typeof node.addDOMWidget !== "function") return;

  const wrap = document.createElement("div");
  wrap.className = "mps-rev";
  const btn = document.createElement("button");
  btn.textContent = "测试模型连接";
  const syncBtn = document.createElement("button");
  syncBtn.textContent = "⇄同步";
  syncBtn.title = "把当前 LLM 连接配置（不含 api_key）复制到另外两个 MOSS 节点；key 建议放 config.json";
  syncBtn.addEventListener("click", () => {
    const n = syncLlmConfig(node, app.graph, app);
    setReverseStatus(node, `已同步 LLM 配置到另外 ${n} 个节点`, "ok");
  });
  const detailBtn = document.createElement("button");
  detailBtn.textContent = "详情";
  detailBtn.disabled = true;
  const runBtn = document.createElement("button");
  runBtn.textContent = "⚡反推";
  runBtn.title = "只提交本反推节点及其上游（LoadImage 等）执行一次；结果在本次执行后写回前端 Editor。完整链路（Editor→Translator→Compiler→生图）需要再 Queue 一次";
  const probeStatus = document.createElement("div");
  probeStatus.className = "mps-rev-status";
  probeStatus.textContent = "未测试";
  const revStatus = document.createElement("div");
  revStatus.className = "mps-rev-status";
  revStatus.textContent = "等待反推…（也可点 ⚡反推 单独执行）";
  wrap.append(btn, runBtn, syncBtn, detailBtn, probeStatus, revStatus);

  const domw = node.addDOMWidget("连接测试", "mps_reverse_probe", wrap, {
    serialize: false,
    hideOnZoom: false,
  });
  domw.computeSize = (width) => [Math.max(Number(width) || 320, 320), 180];
  wrap.addEventListener("pointerdown", (e) => e.stopPropagation());
  node._mpsProbeStatusEl = probeStatus;
  node._mpsReverseStatusEl = revStatus;

  // 明文 key 一次性警告（工作流/历史会保存 widget 的 api_key）
  const showKeyWarn = (t) => {
    try { setReverseStatus(node, "⚠ " + t, "warn"); } catch (e) { /* noop */ }
    try {
      app.extensionManager?.toast?.add({ severity: "warn", summary: "MOSS Studio", detail: t, life: 6000 });
    } catch (e) { /* toast 可选 */ }
  };
  warnApiKeyIfSet(node, showKeyWarn);
  bindApiKeyWarn(node, showKeyWarn);

  let busy = false;
  let controller = null;
  let lastDetail = null;

  const showDetail = () => {
    if (!lastDetail) return;
    const pre = document.createElement("pre");
    pre.style.whiteSpace = "pre-wrap";
    pre.textContent = JSON.stringify(lastDetail, null, 2);
    try {
      app.ui.dialog.show(pre);
    } catch (e) {
      alert(pre.textContent);
    }
  };
  detailBtn.addEventListener("click", showDetail);

  const setBusy = (v) => {
    busy = v;
    btn.disabled = v;
    btn.textContent = v ? "测试中…" : "测试模型连接";
  };

  btn.addEventListener("click", async () => {
    if (busy) return;
    setBusy(true);
    probeStatus.className = "mps-rev-status";
    probeStatus.textContent = "正在请求…（本地模型首次加载可能较慢）";
    detailBtn.disabled = true;
    lastDetail = null;
    controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 180000);
    try {
      const provider = widgetValue(node, "provider") ?? "";
      const mode = normalizeReverseMode(widgetValue(node, "reverse_mode"));
      const manual = isManual(mode);
      const payload = {
        provider,
        base_url: widgetValue(node, "base_url") ?? "",
        model: widgetValue(node, "model") ?? "",
        api_key: widgetValue(node, "api_key") ?? "",
        // 非 manual 的细节控件不参与真实反推，探测也按模式预设计算，避免两边不一致
        thinking: manual ? (widgetValue(node, "thinking") ?? "off") : "off",
        temperature: manual ? widgetValue(node, "temperature") : modeTemperature(mode, provider),
        extra_body: widgetValue(node, "extra_body") ?? "",
        timeout: Number(widgetValue(node, "timeout") ?? 180),
      };
      const resp = await api.fetchApi(ROUTE, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
        timeoutMs: null,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok && !data.error_type) data.error_type = "http_" + resp.status;
      renderProbe(data);
    } catch (e) {
      const timeout = e && (e.name === "AbortError" || e.name === "TimeoutError");
      renderProbe({
        ok: false,
        error_type: timeout ? "client_timeout" : "client_network",
        message: timeout ? "前端等待超时/已取消" : String((e && e.message) || e),
      });
    } finally {
      clearTimeout(timer);
      controller = null;
      setBusy(false);
    }
  });

  function renderProbe(d) {
    const ok = d && d.ok === true;
    const visionIssue = d && d.vision_ok === false && d.text_ok === true;
    const softWarn = ok && (d.warning || d.vision_confirmed === false);
    const kind = ok ? (softWarn ? "warn" : "ok") : visionIssue ? "warn" : "err";
    probeStatus.className = "mps-rev-status " + kind;
    const lines = [
      `${ok ? (softWarn ? "[WARN]" : "[OK]") : visionIssue ? "[WARN]" : "[ERR]"} ${(d && d.message) || (ok ? "连接成功" : "连接失败")}`,
      d && d.warning ? `警告: ${d.warning}` : "",
      d && d.hint ? `提示: ${d.hint}` : "",
      `模型: ${(d && d.model) || "-"}${d && d.response_model && d.response_model !== d.model ? ` (实际: ${d.response_model})` : ""}`,
      `端点: ${(d && d.endpoint) || "-"}`,
      `延迟: ${d && d.latency_ms != null ? d.latency_ms : "-"} ms / HTTP: ${d && d.status != null ? d.status : "-"}`,
      d && d.error_type ? `类型: ${d.error_type}` : "",
      d && d.api_key_masked ? `Key: ${d.api_key_masked}` : "",
    ].filter(Boolean);
    probeStatus.textContent = lines.join("\n");
    lastDetail = d || null;
    detailBtn.disabled = !lastDetail;
    try {
      app.extensionManager?.toast?.add({
        severity: ok ? (softWarn ? "warn" : "success") : visionIssue ? "warn" : "error",
        summary: ok ? (softWarn ? "连接成功但未确认图片" : "模型连接成功")
          : visionIssue ? "连接正常但不支持图片" : "模型连接失败",
        detail: (d && (d.warning || d.message || d.error_type)) || "",
        life: 4000,
      });
    } catch (e) { /* toast 可选 */ }
  }

  let running = false;
  runBtn.addEventListener("click", async () => {
    if (running) return;
    running = true;
    runBtn.disabled = true;
    runBtn.textContent = "⏳反推中…";
    setReverseStatus(node, "正在单独执行反推（含上游 LoadImage）…", "");
    try {
      const a = app.api || (typeof window !== "undefined" && window.app && window.app.api);
      if (!a || !a.fetchApi) throw new Error("前端尚未就绪，稍后再试");
      if (typeof app.graphToPrompt !== "function") throw new Error("当前前端不支持 graphToPrompt");
      const full = await app.graphToPrompt();
      const all = (full && (full.output || full)) || {};
      const subset = collectPromptSubset(all, node.id);
      if (!subset[String(node.id)]) {
        throw new Error("反推节点被旁路或未注册，无法单独执行（检查 Ctrl+B / 节点状态）");
      }
      // 单独执行本次强制启用，不修改节点上的 enabled 勾选（完整 Queue 仍按用户设置）
      subset[String(node.id)].inputs = { ...(subset[String(node.id)].inputs || {}), enabled: true };
      const resp = await a.fetchApi("/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: subset, client_id: a.clientId }),
      });
      if (!resp.ok) {
        const j = await resp.json().catch(() => ({}));
        throw new Error((j.error && j.error.message) || ("HTTP " + resp.status));
      }
      setReverseStatus(node, "已提交单独反推：完成后结果写回 Editor；完整链路请再 Queue 一次", "");
    } catch (e) {
      setReverseStatus(node, "反推提交失败: " + ((e && e.message) || e), "err");
    } finally {
      running = false;
      runBtn.disabled = false;
      runBtn.textContent = "⚡反推";
    }
  });

  const modeW = findWidget(node, "reverse_mode");
  if (modeW) bindModeCallbacks(node);
  applyModeVisibility(node);
  const modeTimer = setInterval(() => {
    try {
      bindModeCallbacks(node);
      syncEnabledFromLinks(node);
      warnApiKeyIfSet(node, showKeyWarn);
      // configure 可能替换 widget 对象或改值；只在模式实际变化时重跑显隐/恢复
      const migrated = migrateWidgetLayout(node);
      const m = normalizeReverseMode(widgetValue(node, "reverse_mode"));
      if (migrated || m !== node._mpsModeApplied) applyModeVisibility(node);
    } catch (e) { /* noop */ }
  }, 1000);
  const prevCfg = node.onConfigure;
  node.onConfigure = function (...a) {
    const r = prevCfg ? prevCfg.apply(this, a) : undefined;
    setTimeout(() => {
      try {
        bindModeCallbacks(node);
        syncEnabledFromLinks(node);
        warnApiKeyIfSet(node, showKeyWarn);
        applyModeVisibility(node);
      } catch (e) { /* noop */ }
    }, 0);
    return r;
  };

  const prevRemove = node.onRemoved;
  node.onRemoved = function (...args) {
    if (controller) controller.abort();
    clearInterval(modeTimer);
    if (prevRemove) return prevRemove.apply(this, args);
  };
}

function applyReverse(node, raw) {
  const payload = parseReversePayload(raw);
  if (!payload) {
    setReverseStatus(node, "反推结果不是合法 JSON", "err");
    return;
  }
  // onExecuted 与 executed 双通道会送同一份结果：仅抑制 1.5s 内的重复事件。
  // 不能永久按 sig 早退，否则用户清空画布/改参数后重新 Queue 同一结果永远写不回来。
  const now = Date.now();
  if (node._mpsLastUiSig === payload.sig && now - (node._mpsLastUiAt || 0) < 1500) return;

  const targets = findTargets(node);
  if (!targets.length) {
    setReverseStatus(node, "未找到下游 Editor：请把输出接到 Editor 的 reverse_in", "err");
    return;
  }
  let applied = 0;
  let skipped = 0;
  const notes = [];
  for (const t of targets) {
    if (t.inputName !== "reverse_in") {
      notes.push(`输出 ${t.slot} 接的是 ${t.inputName || "未知输入"}，请改接 reverse_in`);
      continue;
    }
    const form = pickReverseForm(payload, t.slot);
    if (!form) {
      notes.push(`输出 ${t.slot} 没有对应内容`);
      continue;
    }
    if (typeof t.target._mpsApplyReverse !== "function") {
      notes.push("Editor 尚未初始化，请刷新页面");
      continue;
    }
    const res = t.target._mpsApplyReverse({
      ...form,
      sig: payload.sig,
      width: payload.width,
      height: payload.height,
    });
    if (res && res.skipped === "same") { skipped += 1; notes.push("同一结果已跳过"); }
    else if (res && res.skipped === "edited") { skipped += 1; notes.push("保留手改画布（replace_edits 关闭）"); }
    else if (res && res.ok) applied += 1;
    else notes.push(`写入失败: ${(res && res.error) || "未知"}`);
  }

  const stats = `${payload.items ?? "-"} 项 · ${payload.texts ?? "-"} 文字 · ${payload.width ?? "-"}×${payload.height ?? "-"}`;
  const onlySame = !applied && skipped > 0 && notes.every((n) => n === "同一结果已跳过");
  if (applied) {
    setReverseStatus(
      node,
      `反推完成：${stats} · 已写入 Editor` + (notes.length ? `\n${notes.join("；")}` : ""),
      notes.length ? "warn" : "ok"
    );
  } else if (onlySame) {
    setReverseStatus(node, `反推完成：${stats} · ${notes.join("；")}`, "warn");
  } else if (skipped) {
    // 保留手改：Editor 的 last_applied_sig 不更新，用户打开 replace_edits 后再 Queue 可重试
    setReverseStatus(node, `反推完成：${stats} · ${notes.join("；")}`, "warn");
  } else {
    setReverseStatus(node, `反推完成但未写入：${notes.join("；") || "无可用 Editor"}`, "err");
  }
  node._mpsLastUiSig = payload.sig;
  node._mpsLastUiAt = Date.now();
}

function onApiExecuted(ev) {
  const d = ev && ev.detail;
  if (!d) return;
  const id = String(d.node ?? d.display_node ?? "");
  const node = app.graph?.getNodeById?.(id);
  if (!node || !isReverseNode(node)) return;
  const out = d.output && (d.output.ui || d.output);
  if (out && out.mps_reverse !== undefined) applyReverse(node, out.mps_reverse);
}

let apiHooked = false;
function hookApi() {
  if (apiHooked) return;
  const a = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
  if (!a || !a.addEventListener) return;
  a.addEventListener("executed", onApiExecuted);
  apiHooked = true;
}
hookApi();
let apiTries = 0;
const apiTimer = setInterval(() => {
  hookApi();
  if (apiHooked || ++apiTries >= 20) clearInterval(apiTimer);
}, 1000);

app.registerExtension({
  name: "MOSSPromptStudio.ImageReverse",
  async nodeCreated(node) {
    if (!isReverseNode(node)) return;
    initNode(node);
    const prev = node.onExecuted;
    node.onExecuted = function (info) {
      try {
        const out = info && (info.ui || info);
        if (out && out.mps_reverse !== undefined) applyReverse(node, out.mps_reverse);
      } catch (e) { /* noop */ }
      if (prev) return prev.call(this, info);
    };
  },
});

console.log("[MOSS Studio] 图像反推节点扩展已加载");
