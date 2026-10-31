/**
 * Editor 画布扩展的样式注入（自 editor_canvas.js 机械抽出，行为不变）。
 * CSS 常量 + 一次性 <style> 注入；Editor / Translator 节点共用。
 */
const CSS = `
.mps-root{display:flex;flex-direction:column;gap:6px;font-family:system-ui,'Segoe UI',sans-serif;color:#ddd;font-size:12px;user-select:none;width:100%;}
.mps-toolbar{display:flex;flex-direction:column;gap:4px;background:#232323;border:1px solid #3a3a3a;border-radius:6px;padding:6px;}
.mps-toolbar button{background:#333;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:3px 8px;cursor:pointer;font-size:12px;}
.mps-toolbar button:hover:not(:disabled){background:#454545;}
.mps-toolbar button:disabled{opacity:.4;cursor:default;}
.mps-toolbar select{background:#2a2a2a;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:2px 4px;}
.mps-toolbar select.mps-size{min-width:88px;flex:0 0 auto;}
/* ⚡拆解 主按钮：更大更亮，与普通按钮（灰）一眼区分；蓝降饱和避免刺眼 */
.mps-toolbar button.mps-primary{background:#3d6b9e;border-color:#5580ad;color:#fff;font-size:13px;padding:5px 14px;letter-spacing:.5px;}
.mps-toolbar button.mps-primary:hover:not(:disabled){background:#4d7cae;}
/* 工具栏固定两行：第一行 编辑工具+右端 ⇄同步/⧉复制；第二行 注入/模板+右端 ⚡拆解（正对同步/复制下方） */
.mps-toolbar .mps-tb-line{display:flex;flex-wrap:wrap;gap:5px;align-items:center;}
.mps-toolbar .mps-tb-end{margin-left:auto;display:flex;gap:5px;align-items:center;}
.mps-toolbar .mps-chk{display:flex;align-items:center;gap:3px;color:#bbb;}
.mps-toolbar .mps-spacer{flex:1;}
.mps-main{display:flex;gap:6px;align-items:stretch;flex:1 1 auto;min-height:0;}
.mps-canvasWrap{position:relative;flex:1 1 auto;min-width:0;min-height:120px;background:#101010;border:1px solid #3a3a3a;border-radius:6px;display:flex;justify-content:center;align-items:center;padding:8px;outline:none;overflow:hidden;}
.mps-canvasWrap:focus{border-color:#5a7db0;}
.mps-canvas{position:relative;background:repeating-linear-gradient(0deg,#151515 0 31px,#181818 31px 32px),repeating-linear-gradient(90deg,#151515 0 31px,#181818 31px 32px);border:1px solid #444;}
.mps-underlay{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;opacity:.55;pointer-events:none;z-index:0;}
.mps-box{position:absolute;border:1.5px solid;box-sizing:border-box;cursor:move;background:rgba(255,255,255,.03);z-index:1;}
.mps-box.sel{box-shadow:0 0 0 2px #fff inset;background:rgba(255,255,255,.07);}
.mps-chip{position:absolute;top:-10px;left:2px;font-size:10px;line-height:1;padding:1px 4px;border-radius:3px;background:#000c;white-space:nowrap;pointer-events:none;}
.mps-h{position:absolute;width:9px;height:9px;background:#fff;border:1px solid #000;border-radius:2px;z-index:2;}
.mps-h.nw{left:-5px;top:-5px;cursor:nwse-resize;} .mps-h.ne{right:-5px;top:-5px;cursor:nesw-resize;}
.mps-h.sw{left:-5px;bottom:-5px;cursor:nesw-resize;} .mps-h.se{right:-5px;bottom:-5px;cursor:nwse-resize;}
.mps-capbox{width:100%;min-height:300px;background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:4px;padding:8px;font-size:13px;resize:vertical;font-family:inherit;}
.mps-side{width:292px;flex:0 0 292px;display:flex;flex-direction:column;gap:6px;min-height:0;overflow:auto;}
.mps-panel{background:#232323;border:1px solid #3a3a3a;border-radius:6px;padding:6px;}
.mps-panel h4{margin:0 0 5px;font-size:12px;color:#b9b9b9;font-weight:600;}
.mps-row{display:flex;gap:4px;align-items:center;margin:3px 0;}
.mps-row>label{flex:0 0 58px;color:#a5a5a5;font-size:11px;}
.mps-row>input[type=text],.mps-row>select{flex:1;min-width:0;background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:4px;padding:2px 4px;font-size:12px;}
.mps-textarea{width:100%;box-sizing:border-box;background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:4px;padding:4px;font-size:12px;resize:vertical;font-family:inherit;margin:2px 0;}
/* palette 色块编辑：点色块开原生拾色器，× 删除，＋ 添加 */
.mps-swbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0;}
.mps-sw{position:relative;width:28px;height:22px;border:1px solid #555;border-radius:4px;overflow:hidden;background:#1b1b1b;}
.mps-sw input[type=color]{position:absolute;inset:0;width:100%;height:100%;border:none;padding:0;background:none;cursor:pointer;}
.mps-sw .mps-swx{position:absolute;top:-1px;right:0;width:11px;height:11px;line-height:10px;text-align:center;font-size:9px;color:#fff;background:#000a;border-radius:2px;cursor:pointer;opacity:0;}
.mps-sw:hover .mps-swx{opacity:1;}
.mps-layer{display:flex;gap:4px;align-items:flex-start;padding:3px 4px;border-radius:4px;cursor:pointer;}
.mps-layer:hover{background:#2e2e2e;}
.mps-layer.sel{background:#37517a;}
.mps-layer .dot{width:8px;height:8px;border-radius:2px;flex:0 0 8px;margin-top:4px;}
.mps-layer .cap{flex:1;min-width:0;white-space:normal;overflow-wrap:anywhere;word-break:break-word;line-height:1.35;font-size:11px;}
.mps-mini{background:#333;color:#ddd;border:1px solid #4a4a4a;border-radius:3px;padding:0 5px;font-size:11px;cursor:pointer;}
.mps-mini:hover{background:#454545;}
.mps-overlap-pair{margin:4px 0;padding:4px;background:#1b1b1b;border-radius:4px;font-size:11px;}
.mps-status{padding:4px 8px;border-radius:6px;background:#232323;border:1px solid #3a3a3a;color:#bbb;white-space:pre-wrap;}
.mps-sizer{height:10px;flex:0 0 10px;margin:2px -2px 0;cursor:ns-resize;background:linear-gradient(#2a2a2a,#2a2a2a) center/46px 3px no-repeat;border-radius:3px;}
.mps-sizer:hover{background-color:#3a3a3a;}
.mps-vdivider{flex:0 0 5px;width:5px;cursor:col-resize;background:#2a2a2a;border-radius:2px;}
.mps-vdivider:hover{background:#3a4a6a;}
/* 窄节点自适应：固定 292px 侧栏会把画布挤成一条竖缝（文字逐字竖排），按可用宽度降级 */
.mps-compact .mps-side{width:224px !important;flex-basis:224px !important;}
.mps-narrow .mps-main{flex-direction:column;overflow:hidden;}
.mps-narrow .mps-vdivider{display:none;}
.mps-narrow .mps-side{width:auto !important;flex:0 0 auto !important;max-height:240px;}
.mps-narrow .mps-canvasWrap{min-height:200px;}
.mps-narrow .mps-capbox{min-height:200px;}
.mps-status.err{color:#ff8b8b;border-color:#6b3a3a;}
.mps-status.ok{color:#8fd48f;}
.mps-msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#888;pointer-events:none;padding:20px;text-align:center;}
.mps-msg.err{color:#ff8b8b;white-space:pre-wrap;}
.mps-cfgbar{display:flex;gap:5px;align-items:center;padding:3px 4px;font-family:system-ui,'Segoe UI',sans-serif;font-size:12px;user-select:none;width:100%;}
.mps-cfgbar select{background:#2a2a2a;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:2px 4px;max-width:200px;}
.mps-cfgbar button{background:#333;color:#eee;border:1px solid #4a4a4a;border-radius:4px;padding:2px 8px;cursor:pointer;font-size:12px;}
.mps-cfgbar button:hover{background:#454545;}
.mps-cfgmsg{color:#8fd48f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
`;

export function injectStyle() {
  if (document.getElementById("mps-style")) return;
  const st = document.createElement("style");
  st.id = "mps-style";
  st.textContent = CSS;
  document.head.appendChild(st);
}
