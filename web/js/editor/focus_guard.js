/**
 * 检查器焦点守卫（纯逻辑 + 注入的 DOM 对象，便于 Node 测试）。
 *
 * renderInspector() 会清空 insp.innerHTML。若焦点还在某个输入框里，元素被
 * 移除时浏览器不一定派发 blur —— bindField 的 blur 处理（writeEdits +
 * pushUndo）就丢了，300ms 防抖窗口内的编辑可能未落盘/未进撤销栈。
 * 重绘前主动 blur 一次，让现有处理先跑完。
 */
export function flushFocusedField(activeElement, container) {
  if (!activeElement || typeof activeElement.blur !== "function") return false;
  if (!container || typeof container.contains !== "function") return false;
  if (!container.contains(activeElement)) return false;
  activeElement.blur();
  return true;
}
