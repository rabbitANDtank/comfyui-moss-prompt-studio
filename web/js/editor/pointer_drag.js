/**
 * 指针拖拽的事件绑定（纯逻辑，可注入假 target 做 Node 测试）。
 *
 * 拖拽结束不能只监听 pointerup：拖拽中切窗、触控笔/手势取消、元素丢失
 * pointer capture 时不会收到 pointerup，残留的 pointermove 会让 hover
 * 继续改宽度/高度。这里统一在 pointerup / pointercancel /
 * lostpointercapture 三个事件上清理。
 */

export const DRAG_END_EVENTS = Object.freeze(["pointerup", "pointercancel", "lostpointercapture"]);

/**
 * 挂载一次拖拽：pointermove → onMove；任一结束事件 → 清理并回调 onEnd。
 * 返回 cleanup（可提前解除；重复调用无副作用）。
 */
export function bindPointerDrag(target, { onMove, onEnd } = {}) {
  let active = true;
  const cleanup = () => {
    if (!active) return;
    active = false;
    target.removeEventListener("pointermove", onMove);
    for (const type of DRAG_END_EVENTS) target.removeEventListener(type, end);
  };
  const end = (event) => {
    if (!active) return;
    cleanup();
    if (typeof onEnd === "function") onEnd(event);
  };
  target.addEventListener("pointermove", onMove);
  for (const type of DRAG_END_EVENTS) target.addEventListener(type, end);
  return cleanup;
}
