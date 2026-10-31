/**
 * Editor 画布的姿势库点选面板（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * poseCatalogCache/fetchPoseCatalog（/moss_studio/pose_catalog 代理索引，带缓存）、
 * closePosePicker、disconnectPoseInput（不同前端 API 兼容的连线断开）、
 * writablePoseSource（PrimitiveString 等可写连线源识别）、openPosePicker 与
 * buildPosePickerUI（筛选/搜索/缩略图网格与点选写回 pose_in(_2)）。
 *
 * ctx = { node, M, fx, dom }：fx 为跨块函数注册表（closeTemplatePanel 来自
 * template_panel，打开面板时互斥关闭——按钮点击时才调用）。
 */
import { app } from "/scripts/app.js";
import { poseSourceLabel, poseWriteTarget, poseSourceWritable } from "./pose.js";
import { buildFacets, filterPoses, groupByPose, toDescription, poseThumbUrl } from "./pose_picker.js";

export function mountPosePanel(ctx) {
  const { node, fx } = ctx;
  const { root, runStatus } = ctx.dom;

  // ---------- 姿势库点选面板（v1.7.1） ----------
  // 数据经 /moss_studio/pose_catalog 代理自已安装的 comfyui-pose-gallery（本插件不
  // 携带其索引数据）；未安装时给出指引。选中即把图库同格式描述串写入 pose_in(_2)。
  let poseCatalogCache = null;
  async function fetchPoseCatalog() {
    if (poseCatalogCache) return poseCatalogCache;
    const a = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    const resp = await a.fetchApi("/moss_studio/pose_catalog");
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.message || ("HTTP " + resp.status));
    poseCatalogCache = data;
    return data;
  }

  function closePosePicker() {
    const old = root.querySelector(".mps-posepicker");
    if (old) old.remove();
  }

  // 断开姿势输入的连线（不同前端 API 兼容）；返回是否已断开。
  function disconnectPoseInput(name) {
    const idx = (node.inputs || []).findIndex((i) => i && i.name === name);
    if (idx < 0) return true;
    const inp = node.inputs[idx];
    if (!inp || inp.link == null) return true;
    const lid = inp.link;
    try {
      if (typeof node.disconnectInput === "function") {
        node.disconnectInput(idx);
      } else {
        const g = node.graph || app.graph;
        if (g && typeof g.removeLink === "function") g.removeLink(lid);
      }
    } catch (e) { /* 下面统一回退 */ }
    if (node.inputs[idx] && node.inputs[idx].link != null) {
      try { node.inputs[idx].link = null; } catch (e) { /* noop */ }
      const g = node.graph || app.graph;
      try { if (g && g.links && typeof g.links.delete === "function") g.links.delete(lid); } catch (e) { /* noop */ }
    }
    return !(node.inputs[idx] && node.inputs[idx].link != null);
  }

  // 可写的连线源：PrimitiveString 等常量节点（有字符串 widget）。点选时优先写回
  // 这类源节点并保留连线；Pose Gallery 等计算输出没有可写 widget，返回 null。
  function writablePoseSource(name) {
    const idx = (node.inputs || []).findIndex((i) => i && i.name === name);
    const inp = idx >= 0 ? node.inputs[idx] : null;
    if (!inp || inp.link == null) return null;
    const g = node.graph || app.graph;
    let link = null;
    try {
      if (g && g.links) {
        if (typeof g.links.get === "function") link = g.links.get(inp.link) || g.links.get(String(inp.link));
        else if (Array.isArray(g.links)) link = g.links.find((l) => l && String(l.id) === String(inp.link));
        else link = g.links[inp.link];
      }
    } catch (e) { /* noop */ }
    const origin = link && g && typeof g.getNodeById === "function" ? g.getNodeById(link.origin_id) : null;
    if (!origin || !Array.isArray(origin.widgets)) return null;
    const outName = origin.outputs && origin.outputs[link.origin_slot] && origin.outputs[link.origin_slot].name;
    const nodeType = origin.type || (origin.constructor && origin.constructor.comfyClass) || origin.comfyClass || "";
    for (const nm of [outName, name, "value", "text", "description"]) {
      if (!nm) continue;
      const ww = (origin.widgets || []).find((x) => x && x.name === nm && typeof x.value === "string");
      if (ww && poseSourceWritable({ nodeType, widgetName: ww.name, outName })) {
        return { node: origin, widget: ww };
      }
    }
    // 不再使用“唯一字符串 widget”兜底：Pose Gallery 的 pose_id 不是可写输出，
    // 写进去会让上游 VALIDATE_INPUTS 抛“未知姿势 ID”，后续所有提交 400。
    return null;
  }

  function openPosePicker(slot) {
    fx.closeTemplatePanel();
    closePosePicker();
    const panel = document.createElement("div");
    panel.className = "mps-posepicker";
    panel.style.cssText = "border:1px solid #3a5a3a;border-radius:8px;background:#1c221c;margin:4px 0;padding:8px;";
    const loading = document.createElement("div");
    loading.style.color = "#9c9";
    loading.textContent = "⏳ 正在读取姿势库索引…";
    panel.appendChild(loading);
    root.insertBefore(panel, runStatus);
    const setRun = (t, cls) => { runStatus.textContent = t; runStatus.className = "mps-status mps-runstatus " + (cls || ""); };
    fetchPoseCatalog().then((data) => {
      buildPosePickerUI(panel, data, slot, (item) => {
        const name = slot === 1 ? "pose_in" : "pose_in_2";
        const w = node.widgets.find((x) => x.name === name);
        if (!w) { setRun("❌ 未找到 pose_in widget", "err"); return; }
        const idx = (node.inputs || []).findIndex((i) => i && i.name === name);
        const hasLink = idx >= 0 && node.inputs[idx].link != null;
        const desc = toDescription(item);
        const src = hasLink ? writablePoseSource(name) : null;
        const target = poseWriteTarget({ linked: hasLink, sourceWritable: !!src });
        if (target === "source") {
          // 连线源是可写的常量节点（如 PrimitiveString）：写源节点、保留连线，
          // 并把节点标题标成“姿势人物①/②”，让画布上能直接看到当前点选结果。
          src.widget.value = desc;
          if (src.widget.inputEl) src.widget.inputEl.value = desc;
          const label = poseSourceLabel(slot);
          const oldTitle = String(src.node.title || "").trim();
          if (!oldTitle || /^(字符串|PrimitiveString)$/i.test(oldTitle) || /^姿势人物/.test(oldTitle)) {
            src.node.title = label;
          }
          try { src.node.setDirtyCanvas(true, true); } catch (e) { /* noop */ }
          try { (node.graph || app.graph).setDirtyCanvas(true, true); } catch (e) { /* noop */ }
          // Editor widget 同步一份镜像：以后断开连线仍保留最近点选值
          w.value = desc;
          if (w.inputEl) w.inputEl.value = desc;
          try { node._mpsPoseInputs?.refresh(); } catch (e) { /* noop */ }
          closePosePicker();
          setRun(`🎯 姿势${slot === 1 ? "①" : "②"}已选择：${item.system}·${item.name}（${item.view}）——已写入 ${label}（连线保留）`, "ok");
          return;
        }
        let disconnected = false;
        if (target === "prompt-disconnect") {
          // 连线源不可写（如 Pose Gallery 计算输出）：只能断开后用 widget 值
          const ok = window.confirm(`${name} 当前有连线，且连线源不是可写的字符串节点。是否断开连线并写入当前点选？`);
          if (!ok) {
            setRun(`已取消：${name} 仍有连线，点选值未写入`, "");
            return;
          }
          if (!disconnectPoseInput(name)) {
            setRun(`❌ 断开 ${name} 连线失败，请手动断开后重试`, "err");
            return;
          }
          disconnected = true;
        }
        w.value = desc;
        if (w.inputEl) w.inputEl.value = w.value;
        try { node._mpsPoseInputs?.refresh(); } catch (e) { /* noop */ }
        closePosePicker();
        setRun(`🎯 姿势${slot === 1 ? "①" : "②"}已选择：${item.system}·${item.name}（${item.view}）——已写入 ${name}${disconnected ? "（已断开覆盖用连线）" : ""}`, "ok");
      });
    }).catch((e) => {
      panel.textContent = "";
      const errEl = document.createElement("div");
      errEl.style.color = "#ff8b8b";
      errEl.style.whiteSpace = "pre-wrap";
      errEl.textContent = "❌ " + (e && e.message ? e.message : e);
      panel.appendChild(errEl);
      setRun("❌ 姿势库不可用：" + (e && e.message ? e.message : e), "err");
    });
  }

  function buildPosePickerUI(panel, data, slot, onPick) {
    const items = data.items || [];
    const apiRef = (typeof app !== "undefined" && app.api) || (window.app && window.app.api);
    panel.textContent = "";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px;";
    const headInfo = document.createElement("span");
    headInfo.style.color = "#9c9";
    headInfo.textContent = `姿势${slot === 1 ? "①" : "②"} · 共 ${items.length} 条 / ${groupByPose(items).length} 组`;
    head.appendChild(headInfo);
    const mkSel = (label) => {
      const s = document.createElement("select");
      s.style.cssText = "background:#232323;color:#ccc;border:1px solid #3a3a3a;border-radius:4px;padding:2px 4px;max-width:150px;";
      s.appendChild(new Option(label + "·全部", ""));
      return s;
    };
    const fSys = mkSel("体系"), fCat = mkSel("分类"), fView = mkSel("视角");
    const facets = buildFacets(items);
    facets.systems.forEach((v) => fSys.appendChild(new Option(v, v)));
    facets.categories.forEach((v) => fCat.appendChild(new Option(v, v)));
    facets.views.forEach((v) => fView.appendChild(new Option(v, v)));
    const search = document.createElement("input");
    search.type = "text"; search.placeholder = "搜索：编号/姿势名关键词…";
    search.style.cssText = "background:#232323;color:#ccc;border:1px solid #3a3a3a;border-radius:4px;padding:2px 6px;width:160px;";
    const closeBtn = document.createElement("button");
    closeBtn.className = "mps-mini"; closeBtn.textContent = "✕ 关闭";
    closeBtn.addEventListener("click", closePosePicker);
    const refreshBtn = document.createElement("button");
    refreshBtn.className = "mps-mini"; refreshBtn.textContent = "↻ 刷新索引";
    refreshBtn.title = "重新读取姿势库索引（安装/更新 pose-gallery 后使用）";
    refreshBtn.addEventListener("click", () => {
      poseCatalogCache = null;
      openPosePicker(slot);
    });
    head.append(fSys, fCat, fView, search, refreshBtn, closeBtn);
    panel.appendChild(head);
    const list = document.createElement("div");
    list.style.cssText = "max-height:300px;overflow:auto;display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:6px;";
    panel.appendChild(list);
    const render = () => {
      const rows = filterPoses(items, { system: fSys.value, category: fCat.value, view: fView.value, query: search.value });
      const groups = groupByPose(rows);
      list.textContent = "";
      if (!groups.length) {
        const empty = document.createElement("div");
        empty.style.color = "#888";
        empty.textContent = "无匹配姿势，调整筛选";
        list.appendChild(empty);
        return;
      }
      for (const g of groups.slice(0, 120)) {
        const cell = document.createElement("div");
        cell.style.cssText = "border:1px solid #333;border-radius:6px;padding:6px;background:#202620;display:flex;gap:6px;";
        const thumb = document.createElement("img");
        thumb.loading = "lazy"; thumb.alt = "";
        thumb.style.cssText = "width:44px;height:58px;object-fit:cover;border-radius:4px;flex:none;background:#2a2a2a;";
        if (data.has_thumbs) {
          const first = g.views[0];
          thumb.src = poseThumbUrl(apiRef, first.id);
        }
        const info = document.createElement("div");
        info.style.cssText = "min-width:0;flex:1;";
        const nameEl = document.createElement("div");
        nameEl.style.color = "#ddd";
        nameEl.style.fontSize = "12px";
        nameEl.style.overflow = "hidden";
        nameEl.style.textOverflow = "ellipsis";
        nameEl.style.whiteSpace = "nowrap";
        nameEl.title = `${g.system} / ${g.category} / ${g.name}`;
        nameEl.textContent = g.name;
        const metaEl = document.createElement("div");
        metaEl.style.color = "#888";
        metaEl.style.fontSize = "11px";
        metaEl.textContent = g.system;
        metaEl.appendChild(document.createElement("br"));
        metaEl.appendChild(document.createTextNode(g.category));
        info.append(nameEl, metaEl);
        const views = document.createElement("div");
        views.style.cssText = "display:flex;flex-wrap:wrap;gap:3px;margin-top:3px;";
        for (const v of g.views) {
          const b = document.createElement("button");
          b.className = "mps-mini"; b.textContent = v.view; b.title = toDescription(v);
          b.style.padding = "1px 5px"; b.style.fontSize = "11px";
          b.addEventListener("click", (e) => { e.stopPropagation(); onPick(v); });
          views.appendChild(b);
        }
        info.appendChild(views);
        cell.append(thumb, info);
        list.appendChild(cell);
      }
      if (groups.length > 120) {
        const more = document.createElement("div");
        more.style.cssText = "grid-column:1/-1;color:#888;font-size:12px;";
        more.textContent = `… 仅显示前 120 组（共 ${groups.length} 组），请用筛选/搜索缩小范围`;
        list.appendChild(more);
      }
    };
    for (const s of [fSys, fCat, fView]) s.addEventListener("change", render);
    search.addEventListener("input", render);
    render();
  }

  return { openPosePicker, closePosePicker, fetchPoseCatalog, disconnectPoseInput, writablePoseSource };
}
