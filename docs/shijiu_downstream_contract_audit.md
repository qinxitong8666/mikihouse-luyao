# Shijiu 下游契约审计

本审计固定参考 `qinxitong8666/wawu-product-sync@a36c5eab40bf419562ba03d15c090151698d582a`。该仓库在本项目中只作为“瓦屋上游曾计划写入 Shijiu 后台”的下游实现证据，不作为 MIKI HOUSE 的上游模型、mapper 或业务字段语义来源。

## 已由代码或固定样例证明

| 能力 | 证据位置 | 可采用的结论 |
|---|---|---|
| 目标身份 | `backend_client.py:30-42,64-92` | 默认 API 根地址是 `https://api.wfcorp.cn/shijiu`，请求页面 Origin/Referer 指向 `https://shijiu.wfcorp.cn/`。因此该 client 的目标可明确识别为 Shijiu。 |
| 写入门禁 | `backend_client.py:229-236` | 非 dry-run、显式 `confirm_write` 与固定确认文本三重条件是参考实现的写入门禁。当前 MIKI adapter 更严格：根本不实现写方法。 |
| 商品创建/编辑 | `backend_client.py:309-401` | Shijiu 创建和原生编辑均使用 `/shopapi/Goods/newAddGood`；编辑要求整数商品 ID、完整 `sku_info` 和 `spec_name`。本轮只记录为未来执行器契约，不调用。 |
| 上下架 | `backend_client.py:403-438` | 可由完整商品保存或 `/shopapi/Goods/grounding` 表达；本轮只生成动作计划，不调用。 |
| 商品读取与回读 | `backend_client.py:440-573` | `/shopapi/Goods/index`、`/shopapi/goods/getFormatInfo` 以及三个分类读取路径是现有读取接口。本轮网络客户端只允许这些路径。 |
| 图片上传 | `backend_client.py:238-307` | `/v1/cos/upload` 是 Shijiu 图片上传路径。上传属于写操作，本轮仅生成依赖清单，不调用，也不以错误图片替代缺图。 |
| 原生字段集合 | `DISPOSABLE_SKU_UPDATE_SEMANTICS_CREATE_PAYLOAD.json` | 固定样例给出了商品字段、`spec_name` 和 `sku_info` 字段集合；MIKI mapper 只使用这组已出现字段。 |
| 回读校验 | `transformer.py:478-548` | 可按 `sku_code`/规格关联并校验名称、SKU 数量、主图、规格、价格、库存和图片。仅复用校验思想与 Shijiu 返回字段，不复用瓦屋 mapper。 |
| checkpoint/未知结果处理 | `DISPOSABLE_UPDATE_SEMANTICS_RUNNER.py:110-365` | 状态先持久化、传输结果不明时先回读、验证后才能继续回滚/清理。当前阶段只实现 dry-run checkpoint/resume；未来写执行器必须单独授权。 |

## 明确不采用

- 不导入瓦屋上游 API client、瓦屋商品抓取或瓦屋标准商品模型。
- 不导入 `transformer.build_backend_payload()`；MIKI HOUSE 由自身 `master_catalog.json` 独立映射。
- 不采用 `WAWU-*` SKU、瓦屋价格倍率、瓦屋分类、供应商字段值或任何瓦屋字段含义。
- 不把仓库名、历史任务名或 `MyShop` 类名当作目标身份；目标身份由 `/shijiu` API 根地址和 Shijiu 管理端 Origin/Referer 证明。

## 当前 main 缺失的证据与处理

参考仓库提交的 `LIVE_001_EVIDENCE.md/json` 明确记录 `BLOCKED_BEFORE_WRITE`、`backend_write_calls=0`。因此当前 main 能证明目标 URL、接口代码、原生 payload 样例、门禁及离线生命周期测试，但**不能证明该提交曾成功完成真实 Shijiu 创建/更新/回滚闭环**。

本轮据此停止所有真实写入实现：不提供 write CLI、不调用上传/创建/编辑/上下架/删除接口、不生成可直接执行的写请求。报告中的 payload 只是字段映射预览；图片仍是 MIKI 官网源 URL，未来必须先经另行授权的 Shijiu COS 上传并回填，品牌 `brand_id` 也因缺少已证实的 Shijiu 品牌 discovery 契约而保持空值。MIKI 原始品牌会保留在 adapter envelope 与描述字段中供人工复核，但不会猜测品牌 ID。

## 本轮 Shijiu 只读事实

`/shopapi/Goodtype/typeindex` 返回的当前分类树中，规范化名称唯一匹配 MIKI HOUSE 的子类目为 `MikiHouse`：ID `294884`、父类目 ID `288338`（`母婴用品`）。因此本项目固定 `source=MIKIHOUSE` 且所有可发布商品统一写入 `good_type=294884`；官网品牌和分类字段不参与 Shijiu 路由。该类目与 WAWU 的商品身份、SKU 前缀、映射状态及类目选择完全隔离。
