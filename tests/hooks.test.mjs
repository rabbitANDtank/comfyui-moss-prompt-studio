import test from "node:test";
import assert from "node:assert/strict";
import {
  createEditorFlushRegistry, mountEditorLifecycle, mountCompilerSync,
} from "../web/js/editor/comfy-hooks.js";

test("flush registry wraps queuePrompt once and unregisters", () => {
  const reg = createEditorFlushRegistry();
  const calls = [];
  const unregister = reg.register(() => calls.push("flush"));
  const app = { queuePrompt: () => calls.push("orig") };

  reg.install(app);
  app.queuePrompt();
  assert.deepEqual(calls, ["flush", "orig"]);

  // 第二次 install 不应重复包一层
  calls.length = 0;
  reg.install(app);
  app.queuePrompt();
  assert.deepEqual(calls, ["flush", "orig"]);

  calls.length = 0;
  unregister();
  app.queuePrompt();
  assert.deepEqual(calls, ["orig"]);
  assert.equal(reg.size, 0);
});

test("mountEditorLifecycle preserves and orders previous handlers", () => {
  const calls = [];
  const node = {
    onConfigure: () => calls.push("prevConfigure"),
    onResize: () => calls.push("prevResize"),
    onRemoved: () => calls.push("prevRemoved"),
  };
  mountEditorLifecycle(node, {
    onConfigure: () => calls.push("configure"),
    onResize: () => calls.push("resize"),
    onRemoved: () => calls.push("removed"),
  });
  node.onConfigure(1, 2);
  node.onResize(3);
  node.onRemoved(4);
  assert.deepEqual(calls, ["prevConfigure", "configure", "resize", "prevResize", "removed", "prevRemoved"]);
});

test("mountCompilerSync follows draft upstream, auto-wires and syncs size", () => {
  const graph = { links: new Map(), getNodeById(id) { return this.nodes[id]; } };
  graph.nodes = {};

  const editor = {
    id: "1", type: "MOSSEditor",
    inputs: [],
    outputs: [
      { name: "draft", type: "STRING", links: [2] },
      { name: "width", type: "INT", links: [] },
      { name: "height", type: "INT", links: [] },
    ],
    widgets: [
      { name: "edits", value: JSON.stringify({ width: 928, height: 1648 }) },
      { name: "canvas_width", value: 928 },
      { name: "canvas_height", value: 1648 },
    ],
    connect(outSlot, target, inSlot) {
      const lid = 100 + outSlot + inSlot;
      const link = { id: lid, origin_id: this.id, origin_slot: outSlot, target_id: target.id, target_slot: inSlot, type: "INT" };
      target.inputs[inSlot].link = lid;
      this.outputs[outSlot].links.push(lid);
      graph.links.set(lid, link);
      return link;
    },
  };
  const translator = {
    id: "2", type: "MOSSTranslator",
    inputs: [{ name: "draft", link: 2 }],
    outputs: [{ name: "draft_en", type: "STRING", links: [1] }],
    widgets: [],
  };
  const compiler = {
    id: "3", type: "MOSSCompiler", graph,
    inputs: [
      { name: "draft", link: 1 },
      { name: "canvas_width", link: null },
      { name: "canvas_height", link: null },
    ],
    outputs: [
      { name: "prompt", links: null }, { name: "negative", links: null },
      { name: "width", links: null }, { name: "height", links: null },
      { name: "clip_text", links: null },
    ],
    widgets: [{ name: "width", value: 1024 }, { name: "height", value: 1024 }],
    getInputLink(idx) {
      const lid = this.inputs[idx].link;
      return lid == null ? null : graph.links.get(lid);
    },
    setDirtyCanvas() {},
  };
  graph.nodes = { 1: editor, 2: translator, 3: compiler };
  graph.links.set(1, { id: 1, origin_id: "2", origin_slot: 0, target_id: "3", target_slot: 0 });
  graph.links.set(2, { id: 2, origin_id: "1", origin_slot: 0, target_id: "2", target_slot: 0 });

  const handle = mountCompilerSync(compiler);
  handle.syncOnce();
  assert.equal(compiler.inputs[1].link != null, true, "canvas_width 应自动接线");
  assert.equal(compiler.inputs[2].link != null, true, "canvas_height 应自动接线");
  assert.deepEqual(compiler.widgets.map((w) => w.value), [928, 1648]);
  handle.dispose();
});

// ---------- F3：flush 否决 / Queue 互斥 ----------

test("flushAll reports veto and wrapped queuePrompt is not called", () => {
  const reg = createEditorFlushRegistry();
  const calls = [];
  reg.register(() => { calls.push("flush-a"); });
  reg.register(() => { calls.push("flush-b"); return false; });
  const app = { queuePrompt: () => { calls.push("orig"); return "orig-result"; } };
  reg.install(app);

  const ret = app.queuePrompt(0, 1);
  assert.equal(ret, false, "被否决时返回 false");
  assert.deepEqual(calls, ["flush-a", "flush-b"], "原始 queuePrompt 不得被调用");
  assert.equal(reg.flushAll(), false, "flushAll 直接调用也返回 false");
});

test("flushAll returns true and passes through without veto", () => {
  const reg = createEditorFlushRegistry();
  const calls = [];
  reg.register(() => { calls.push("flush"); });           // undefined = 不否决
  reg.register(() => true);                               // true = 不否决
  const app = { queuePrompt: () => { calls.push("orig"); return "ok"; } };
  reg.install(app);

  assert.equal(reg.flushAll(), true);
  calls.length = 0;
  assert.equal(app.queuePrompt(), "ok");
  assert.deepEqual(calls, ["flush", "orig"]);
});

test("Ctrl+Enter veto prevents default and stops the native shortcut", () => {
  const listeners = [];
  const prevDoc = globalThis.document;
  globalThis.document = { addEventListener: (type, fn) => listeners.push({ type, fn }) };
  try {
    const reg = createEditorFlushRegistry();
    const calls = [];
    reg.register(() => { calls.push("flush"); return false; });
    const app = { queuePrompt: () => calls.push("orig") };
    reg.install(app);
    const kd = listeners.find((l) => l.type === "keydown");
    assert.ok(kd, "keydown 监听已安装");
    const ev = {
      ctrlKey: true, key: "Enter", prevented: false, stopped: false,
      preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; },
    };
    kd.fn(ev);
    assert.equal(ev.prevented, true);
    assert.equal(ev.stopped, true);
    assert.deepEqual(calls, ["flush"], "否决时不应触发原始 queuePrompt");
  } finally {
    if (prevDoc === undefined) delete globalThis.document;
    else globalThis.document = prevDoc;
  }
});
