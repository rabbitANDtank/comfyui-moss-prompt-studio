/** 姿势点选面板的纯逻辑（无 DOM，node --test 覆盖）。
 * 数据来自后端 /moss_studio/pose_catalog 代理（已装 comfyui-pose-gallery 时可用），
 * item 字段：{id, system, category, name, view, search}。 */


/** 全部 / 体系 / 分类 / 视角 的去重列表（保持数据出现顺序，"全部"由 UI 层加）。 */
export function buildFacets(items) {
  const systems = [], categories = [], views = [];
  for (const it of items || []) {
    if (!systems.includes(it.system)) systems.push(it.system);
    if (!categories.includes(it.category)) categories.push(it.category);
    if (!views.includes(it.view)) views.push(it.view);
  }
  return { systems, categories, views };
}

/** 按 体系/分类/视角 + 关键词（匹配姿势名/检索串/id）过滤。空筛选 = 全部。 */
export function filterPoses(items, { system = "", category = "", view = "", query = "" } = {}) {
  const q = String(query || "").trim().toLowerCase();
  return (items || []).filter((it) => {
    if (system && it.system !== system) return false;
    if (category && it.category !== category) return false;
    if (view && it.view !== view) return false;
    if (q) {
      const hay = `${it.name || ""} ${it.search || ""} ${it.id || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

/** 选中项 → 与图库 description 输出同格式的姿势参考串（"体系 / 分类 / 姿势名称 / 视角"）。 */
export function toDescription(item) {
  if (!item) return "";
  return [item.system, item.category, item.name, item.view].filter(Boolean).join(" / ");
}

/** 同一姿势组（同体系+编号）按视角去重分组：面板按"姿势"展示，行内列出可选视角。
 * id 形如 full_single_P031_front → group = id 去掉末尾 _视角后缀。 */
export function groupByPose(items) {
  const map = new Map();
  for (const it of items || []) {
    const gid = String(it.id || "").replace(/_(front|high|low|side|diag)$/, "");
    if (!map.has(gid)) {
      map.set(gid, { key: it.system + " / " + it.name, system: it.system,
        category: it.category, name: it.name, views: [] });
    }
    map.get(gid).views.push(it);
  }
  return [...map.values()];
}

/** 缩略图 URL：优先 api.apiURL（子路径部署），其次 api.api_base，最后退回根路径。 */
export function poseThumbUrl(api, id) {
  const path = `/pose_gallery/assets/thumbs/${encodeURIComponent(String(id ?? ""))}.jpg`;
  if (api && typeof api.apiURL === "function") return api.apiURL(path);
  const base = api && typeof api.api_base === "string" ? api.api_base : "";
  if (base) return base.replace(/\/+$/, "") + path;
  return path;
}
