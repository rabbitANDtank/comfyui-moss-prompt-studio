/**
 * 布局版式模板库（纯数据 + 纯查询，无 DOM，node --test 覆盖）。
 *
 * 所有模板均为本项目自建的示例版式与占位文案，遵循官方 layout 草稿契约：
 * width/height 为 16 的倍数，bbox_coord_space="abs"、bbox_format="xyxy"，
 * items 顺序 0 = 画面最前。模板只是起点，载入后按需改 caption/text。
 *
 * 不复制任何第三方项目的模板数据或文案（见 MOSS-IMAGE-NODES-RESEARCH.md 许可红线）。
 */

/** @type {{id:string,name:string,description:string,draft:object}[]} */
export const TEMPLATES = [
  {
    id: "vertical_poster",
    name: "竖版海报",
    description: "主标题 + 中央主体 + 副标题，适合 2:3 竖版宣传图。",
    draft: {
      width: 1024,
      height: 1536,
      bbox_coord_space: "abs",
      bbox_format: "xyxy",
      summary: {
        caption: "示例版式：竖版海报，顶部大标题、中央主体人物、下方副标题，留白清晰。",
        meta: ["poster", "studio photography"],
        canvas: "single",
        palette: ["#1b1b2f", "#e8c07d", "#f4f1ea"],
      },
      items: [
        {
          label: "text",
          caption: "海报主标题：粗体无衬线大字，水平居中",
          text: "主标题示例",
          bbox: [96, 96, 928, 288],
        },
        {
          label: "character",
          caption: "画面主体：一位穿深色风衣的时尚人物，站姿，背景简洁",
          bbox: [160, 352, 864, 1184],
        },
        {
          label: "text",
          caption: "海报副标题：小号说明文字，居中排布",
          text: "副标题示例文字",
          bbox: [192, 1248, 832, 1408],
        },
      ],
    },
  },
  {
    id: "square_social",
    name: "方形社媒图",
    description: "主体居中 + 顶部标题 + 右下角角标，适合 1:1 社媒配图。",
    draft: {
      width: 1024,
      height: 1024,
      bbox_coord_space: "abs",
      bbox_format: "xyxy",
      summary: {
        caption: "示例版式：方形社媒图，主体居中，顶部标题，右下角角标。",
        meta: ["social media post", "square composition"],
        canvas: "single",
        palette: ["#12343b", "#f2b134", "#f7f7f2"],
      },
      items: [
        {
          label: "text",
          caption: "社媒标题：顶部醒目大字，左对齐或居中",
          text: "社媒标题示例",
          bbox: [64, 64, 960, 224],
        },
        {
          label: "object",
          caption: "画面主体：居中陈列的核心物件，轮廓清晰、光影柔和",
          bbox: [128, 288, 896, 880],
        },
        {
          label: "text",
          caption: "角标：右下角小号标签，补充账号名或活动信息",
          text: "角标示例",
          bbox: [640, 880, 960, 976],
        },
      ],
    },
  },
  {
    id: "ecommerce_hero",
    name: "电商主图",
    description: "产品居中 + 卖点文字 + 价格角标，适合商品详情首图。",
    draft: {
      width: 1024,
      height: 1024,
      bbox_coord_space: "abs",
      bbox_format: "xyxy",
      summary: {
        caption: "示例版式：电商主图，产品居中，左上卖点文字，右上价格角标。",
        meta: ["product photography", "e-commerce hero"],
        canvas: "single",
        palette: ["#f4f1ea", "#d94f3d", "#2b2b2b"],
      },
      items: [
        {
          label: "text",
          caption: "卖点文字：左上角短句，突出核心卖点",
          text: "卖点文字示例",
          bbox: [64, 80, 640, 208],
        },
        {
          label: "text",
          caption: "价格角标：右上角价格标签，数字醒目",
          text: "价格 ¥99",
          bbox: [640, 64, 960, 224],
        },
        {
          label: "object",
          caption: "产品主体：居中摆放的商品，干净背景，柔和投影",
          bbox: [192, 224, 832, 816],
        },
      ],
    },
  },
  {
    id: "magazine_cover",
    name: "杂志封面",
    description: "人物 + 大标题 + 期号，适合 2:3 封面构图。",
    draft: {
      width: 1024,
      height: 1536,
      bbox_coord_space: "abs",
      bbox_format: "xyxy",
      summary: {
        caption: "示例版式：杂志封面，顶部大标题，中央人物，左下期号。",
        meta: ["magazine cover", "editorial portrait"],
        canvas: "single",
        palette: ["#20232a", "#e6d5b8", "#c0392b"],
      },
      items: [
        {
          label: "text",
          caption: "封面大标题：顶部横向大字，字重醒目",
          text: "大标题示例文字",
          bbox: [64, 64, 960, 240],
        },
        {
          label: "character",
          caption: "封面人物：半身肖像，视线看向镜头，背景简洁",
          bbox: [128, 272, 896, 1312],
        },
        {
          label: "text",
          caption: "期号：左下角小号文字，标明期数与主题",
          text: "第 01 期 · 示例",
          bbox: [64, 1376, 640, 1488],
        },
      ],
    },
  },
  {
    id: "character_card",
    name: "人物介绍卡",
    description: "左侧角色图 + 右侧名字与简介，适合 3:4 角色卡。",
    draft: {
      width: 768,
      height: 1024,
      bbox_coord_space: "abs",
      bbox_format: "xyxy",
      summary: {
        caption: "示例版式：人物介绍卡，左侧角色立绘，右侧名字与简介。",
        meta: ["character card", "profile layout"],
        canvas: "single",
        palette: ["#2d2a32", "#d8c3a5", "#8a817c"],
      },
      items: [
        {
          label: "character",
          caption: "角色立绘：左侧全身或半身形象，姿态自然",
          bbox: [48, 96, 432, 800],
        },
        {
          label: "text",
          caption: "角色名字：右侧顶部大字，与角色对齐",
          text: "示例角色",
          bbox: [464, 96, 752, 208],
        },
        {
          label: "text",
          caption: "角色简介：右侧正文，简述身份、性格与关键特征",
          text: "简介：角色身份、性格与关键特征。",
          bbox: [464, 256, 752, 928],
        },
      ],
    },
  },
  {
    id: "four_panel_storyboard",
    name: "四格分镜",
    description: "2×2 四格分镜，每格一个场景 + 一条台词文字区。",
    draft: {
      width: 1024,
      height: 1024,
      bbox_coord_space: "abs",
      bbox_format: "xyxy",
      summary: {
        caption: "示例版式：2×2 四格分镜，每格一个场景与一格台词文字区。",
        meta: ["storyboard", "comic panel"],
        canvas: "multi_panel",
        palette: ["#1f1f1f", "#f0e6d2", "#4a6fa5"],
      },
      items: [
        {
          label: "text",
          caption: "第一格台词：格内底部文字区",
          text: "第一格台词",
          bbox: [56, 384, 480, 480],
        },
        {
          label: "text",
          caption: "第二格台词：格内底部文字区",
          text: "第二格台词",
          bbox: [552, 384, 976, 480],
        },
        {
          label: "text",
          caption: "第三格台词：格内底部文字区",
          text: "第三格台词",
          bbox: [56, 880, 480, 976],
        },
        {
          label: "text",
          caption: "第四格台词：格内底部文字区",
          text: "第四格台词",
          bbox: [552, 880, 976, 976],
        },
        {
          label: "object",
          caption: "第一格：场景与人物动作，交代开场",
          bbox: [32, 32, 496, 496],
        },
        {
          label: "object",
          caption: "第二格：场景推进，突出冲突或变化",
          bbox: [528, 32, 992, 496],
        },
        {
          label: "object",
          caption: "第三格：场景转折，给出关键动作",
          bbox: [32, 528, 496, 992],
        },
        {
          label: "object",
          caption: "第四格：场景收束，交代结果或情绪",
          bbox: [528, 528, 992, 992],
        },
      ],
    },
  },
];

/** 模板列表（元数据浅拷贝；draft 为只读共享数据，调用方不要原地修改）。 */
export function listTemplates() {
  return TEMPLATES.map((t) => ({
    id: t.id,
    name: t.name,
    description: t.description,
    draft: t.draft,
  }));
}

/** 按 id 取模板；未知 id 返回 null。 */
export function templateById(id) {
  const key = String(id ?? "");
  return TEMPLATES.find((t) => t.id === key) || null;
}
