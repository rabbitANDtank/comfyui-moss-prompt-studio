/**
 * Editor 画布的版式模板面板与拆解/注入按钮复位（自 editor_canvas.js 机械抽出，行为不变）。
 *
 * resetSplitBtn（拆解/姿势/服装提交按钮与归属状态复位）、版式模板库面板
 * openTemplatePanel/closeTemplatePanel/applyTemplate（模板数据本就在 editor/templates.js，
 * 这里只负责面板 DOM 与载入路径；载入走 adoptEdits 保留撤销历史）。
 *
 * ctx = { node, M, fx, dom, rebindWidgets, get wEdits(){...} }：M 为 node._mps 状态袋
 * （同一对象）；fx 为跨块函数注册表（snapshot/pushUndo/setWidgetInput/adoptEdits/
 * updateToolbar 来自 items/edits_sync，closePosePicker 来自 pose_panel——均为按钮
 * 点击时才调用）；widget 通过 getter 注入以保持 rebindWidgets 换绑后的实时引用。
 */
import { listTemplates } from "./templates.js";

export function mountTemplatePanel(ctx) {
  const { M, fx } = ctx;
  const { root, status, runStatus } = ctx.dom;

  // ⚡拆解按钮：只提交编辑器单节点（OUTPUT_NODE），结果走事件总线写回
  // （顺带复位 📥姿势 按钮：两者共用 mps_draft 写回与错误/超时解除路径）
  function resetSplitBtn() {
    if (M.splitBtn) { M.splitBtn.disabled = false; M.splitBtn.textContent = "⚡拆解"; M.splitBtn = null; }
    if (M.poseBtn) { M.poseBtn.disabled = false; M.poseBtn.textContent = "📥姿势"; M.poseBtn = null; }
    if (M.costumeBtn) { M.costumeBtn.disabled = false; M.costumeBtn.textContent = "📥服装"; M.costumeBtn = null; }
    // 提交已结束（结果到达/失败/超时/队列空）：清掉本次提交的 source 归属记录
    M.splitSrcSig = "";
    M.poseSrcSig = "";
    M.costumeSrcSig = "";
    M.submitting = false;
    // 最近一次手工提交也随本次提交结束而失效：之后到达的完整 Queue 结果没有
    // 匹配对象，回退到 mps_src_sig/pending 判断，不会被 prompt_id 误杀（P1）。
    M.lastSubmitId = "";
  }

  // ---------- 版式模板库（自建示例数据） ----------
  // 模板数据在 editor/templates.js（纯逻辑、Node 测试覆盖）；这里只负责面板 DOM 与
  // 载入路径。载入走 adoptEdits：edits widget 保留 2 空格缩进的模板 JSON，
  // M.S / edits_source_sig / 撤销栈保持一致，source 为空时不会被 700ms 轮询覆盖。
  function closeTemplatePanel() {
    const old = root.querySelector(".mps-templatepicker");
    if (old) old.remove();
  }

  function openTemplatePanel() {
    fx.closePosePicker();
    closeTemplatePanel();
    const panel = document.createElement("div");
    panel.className = "mps-templatepicker";
    panel.style.cssText = "border:1px solid #3a4a5a;border-radius:8px;background:#1a2026;margin:4px 0;padding:8px;";
    const head = document.createElement("div");
    head.style.cssText = "display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px;";
    const title = document.createElement("span");
    title.style.cssText = "color:#9bb7d4;font-weight:600;";
    title.textContent = "📐 版式模板（自建示例）";
    const info = document.createElement("span");
    info.style.cssText = "color:#888;font-size:11px;flex:1;";
    info.textContent = "占位文案仅作示例，载入后按需修改；可 ↶撤销";
    const closeBtn = document.createElement("button");
    closeBtn.className = "mps-mini";
    closeBtn.textContent = "✕ 关闭";
    closeBtn.title = "关闭模板面板";
    closeBtn.addEventListener("click", closeTemplatePanel);
    head.append(title, info, closeBtn);
    panel.appendChild(head);
    const list = document.createElement("div");
    list.style.cssText = "display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:6px;";
    panel.appendChild(list);
    for (const t of listTemplates()) {
      const card = document.createElement("div");
      card.style.cssText = "border:1px solid #333;border-radius:6px;padding:6px;background:#20262b;display:flex;flex-direction:column;gap:4px;";
      const nameEl = document.createElement("div");
      nameEl.style.cssText = "color:#ddd;font-weight:600;font-size:12px;";
      nameEl.textContent = t.name;
      const metaEl = document.createElement("div");
      metaEl.style.cssText = "color:#8fb3d9;font-size:11px;";
      metaEl.textContent = `${t.draft.width}×${t.draft.height} · ${t.draft.items.length} 项 · ${t.draft.summary.canvas || "single"}`;
      const descEl = document.createElement("div");
      descEl.style.cssText = "color:#999;font-size:11px;line-height:1.4;";
      descEl.textContent = t.description;
      const useBtn = document.createElement("button");
      useBtn.className = "mps-mini";
      useBtn.textContent = "使用";
      useBtn.style.alignSelf = "flex-start";
      useBtn.title = "载入该版式到编辑画布（会先记录撤销点）";
      useBtn.addEventListener("click", () => applyTemplate(t));
      card.append(nameEl, metaEl, descEl, useBtn);
      list.appendChild(card);
    }
    root.insertBefore(panel, runStatus);
  }

  function applyTemplate(t) {
    if (!t || !t.draft) return;
    if (!ctx.rebindWidgets()) {
      status.textContent = "模板载入失败：Editor widget 不完整";
      status.className = "mps-status err";
      return;
    }
    const raw = JSON.stringify(t.draft, null, 2);
    const hadCanvas = !!M.S;
    const same = hadCanvas && M.S.kind === "layout"
      && JSON.stringify(M.S.d) === JSON.stringify(t.draft);
    if (same) {
      status.textContent = `已载入模板：${t.name}（与当前画布相同）`;
      status.className = "mps-status";
      closeTemplatePanel();
      return;
    }
    // 先记录载入前的画布；adoptEdits 会按 preRecorded 跳过重复压栈，保留完整撤销历史
    const prevEdits = ctx.wEdits ? ctx.wEdits.value : "";
    if (hadCanvas) fx.pushUndo(fx.snapshot());
    fx.setWidgetInput(ctx.wEdits, raw);
    const ok = fx.adoptEdits(true, { keepUndo: hadCanvas, preRecorded: true });
    if (!ok) {
      if (hadCanvas && M.undo.length) M.undo.pop();
      fx.setWidgetInput(ctx.wEdits, prevEdits);
      status.textContent = "模板载入失败：模板数据无法解析";
      status.className = "mps-status err";
      return;
    }
    status.textContent = `已载入模板：${t.name}${hadCanvas ? "（可 ↶撤销）" : ""}`;
    status.className = "mps-status ok";
    fx.updateToolbar();
    closeTemplatePanel();
  }

  return { resetSplitBtn, openTemplatePanel, closeTemplatePanel };
}
