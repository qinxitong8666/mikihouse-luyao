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

## 当前 main 的真实验证证据与边界

参考仓库提交的 `LIVE_001_EVIDENCE.md/json` 明确记录 `BLOCKED_BEFORE_WRITE`、`backend_write_calls=0`，所以它只能证明目标 URL、接口代码、原生 payload 样例、门禁及离线生命周期测试，不能单独证明成功创建闭环。

本轮用户另行明确授权冻结首批 20 件真实验证后，项目新增独立 fail-closed writer。2026-09-04 实际完成首件 12 张图片上传，目标端均返回 `cdn0.19mini.com` HTTPS URL；随后只发送 1 次 `/shopapi/Goods/newAddGood`，响应为 `code=200, msg=success, data=[]`。该响应没有商品 ID，覆盖上下架/可见状态的精确 MIKI SKU 查询及延迟复核均为 0 条，因此创建结果不能被确认，更无法执行要求的商品/SKU ID 回读闭环。执行器随即冻结 checkpoint：确认创建 0 件、mapping 绑定 0 件、后续 19 件写入 0、legacy cleanup 0。详情见 `deliverables/shijiu_import/first_live_batch_report.json` 和 `first_live_batch_forensics.json`。

这次验证新增了“图片上传链路真实可达且返回目标 URL”的证据，但仍没有“创建成功并可按稳定 MIKI SKU 回读”的证据。不得把 `code=200` 或 `msg=success` 单独解释为创建成功，也不得基于商品名、时间或列表位置猜测 ID。冻结批次禁止自动二次创建。品牌 `brand_id` 仍因没有已证实的品牌 discovery 契约而保持空值。

## 本轮 Shijiu 只读事实

`/shopapi/Goodtype/typeindex` 返回的当前分类树中，规范化名称唯一匹配 MIKI HOUSE 的子类目为 `MikiHouse`：ID `294884`、父类目 ID `288338`（`母婴用品`）。因此本项目固定 `source=MIKIHOUSE` 且所有可发布商品统一写入 `good_type=294884`；官网品牌和分类字段不参与 Shijiu 路由。该类目与 WAWU 的商品身份、SKU 前缀、映射状态及类目选择完全隔离。

该类目现有 286 件商品只归类为 `legacy_reference_only`。本轮仅读取完整列表以形成未来独立下架目标，并均匀抽取 6 件读取详情结构；没有用商品名、SKU、价格或任何内容与 MIKI HOUSE 主库做关联。只读结果确认列表含 `good_name`、`master_graph`、`orderby` 等字段，详情含 `broadcast`、`good_detail_pics`、`good_details`、`spec_name`、`sku_info`，SKU 行实际使用 `spec_son_name`、`price`、`stock`、`sku_code`、`sku_thumbnail` 等字段，另观察到 `serial_number` 等排序字段。可追踪审计只保存字段名、类型、长度/数量统计和目标 ID，不复制旧商品名称、图片、详情或规格内容。

`special_skus_2026aw.csv` 的 351 个品番另属 PDF 专用池，与 legacy 和新商品池均相互独立。Shijiu 计划在任何目标读取之前同时检查主库和增量事件，任一命中即以 `PDF_SPECIAL_LIST` fail closed；在线 311 件和当前离线 40 件采用同一永久规则，未来恢复上架也不能进入 CREATE、UPDATE、库存、图片、价格或恢复流程。
