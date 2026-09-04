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

## 单件原生语义恢复审计

`qinxitong8666/wawu-product-sync@a36c5eab40bf419562ba03d15c090151698d582a` 中的 `DISPOSABLE_SKU_UPDATE_SEMANTICS_CREATE_PAYLOAD.json` 明确使用 `state="1"`、`is_shelf=0`。本仓库以 `config/shijiu_native_create_contract.json` 固定该来源提交、原文件 SHA-256、创建端点以及商品/SKU/规格字段顺序。恢复 payload 只把首次执行器的 `state="0"` 改成原生样例的 `state="1"`，保持 `is_shelf=0` 和所有 MIKIHOUSE 字段不变。

恢复前只读审计覆盖分类树、34 组跨类目/状态的精确 SKU 与名称查询，以及 MikiHouse 类目 11 组过滤视图的完整分页。默认视图为 286 条，所有非空视图 ID 集一致，未发现 `00-1000-028` 的精确 SKU、名称、checkpoint ID 或 mapping ID，因而允许消耗一次单件恢复写预算。

恢复创建复用了首次运行已上传的 12 张 `cdn0.19mini.com` 图片，没有调用 `/v1/cos/upload`；只向 `/shopapi/Goods/newAddGood` 写入一次 `00-1000-028`，没有处理后续 19 件或 legacy。响应再次为 `code=200, msg=success, data=[]`。创建后多轮延迟查询及独立事后取证均显示精确 SKU/名称为 0，默认类目仍为同一组 286 个 ID。由于列表没有暴露候选 product ID，`getFormatInfo` 无法安全调用，商品 ID、SKU ID 和详情闭环均未成立，mapping 因此保持未绑定。

恢复 checkpoint 当前为 `STOPPED_ON_RECOVERY_ERROR`，唯一创建预算已经消耗，禁止幂等重试逻辑再次创建。现有证据只能说明 `state=1` 未解决空响应与不可观测问题，不能推断服务端具体拒绝原因。完整证据位于 `first_product_residual_scan.json`、`first_product_recovery_report.json`、`first_product_recovery_forensics.json`、`first_product_recovery_readback.json` 和 `state/shijiu_first_product_recovery_checkpoint.json`。

## 本轮 Shijiu 只读事实

`/shopapi/Goodtype/typeindex` 返回的当前分类树中，规范化名称唯一匹配 MIKI HOUSE 的子类目为 `MikiHouse`：ID `294884`、父类目 ID `288338`（`母婴用品`）。因此本项目固定 `source=MIKIHOUSE` 且所有可发布商品统一写入 `good_type=294884`；官网品牌和分类字段不参与 Shijiu 路由。该类目与 WAWU 的商品身份、SKU 前缀、映射状态及类目选择完全隔离。

该类目现有 286 件商品只归类为 `legacy_reference_only`。本轮仅读取完整列表以形成未来独立下架目标，并均匀抽取 6 件读取详情结构；没有用商品名、SKU、价格或任何内容与 MIKI HOUSE 主库做关联。只读结果确认列表含 `good_name`、`master_graph`、`orderby` 等字段，详情含 `broadcast`、`good_detail_pics`、`good_details`、`spec_name`、`sku_info`，SKU 行实际使用 `spec_son_name`、`price`、`stock`、`sku_code`、`sku_thumbnail` 等字段，另观察到 `serial_number` 等排序字段。可追踪审计只保存字段名、类型、长度/数量统计和目标 ID，不复制旧商品名称、图片、详情或规格内容。

`special_skus_2026aw.csv` 的 351 个品番另属 PDF 专用池，与 legacy 和新商品池均相互独立。Shijiu 计划在任何目标读取之前同时检查主库和增量事件，任一命中即以 `PDF_SPECIAL_LIST` fail closed；在线 311 件和当前离线 40 件采用同一永久规则，未来恢复上架也不能进入 CREATE、UPDATE、库存、图片、价格或恢复流程。

## 单候选最小 native create 诊断

在永久冻结 `00-1000-028` 后，诊断器从当前非特殊商品池的 618 个“单 variant、当前可售、有图、有正价且未绑定”候选中确定性选择图片最少且品番排序最前的 `17-1366-244`。官网在线回读确认商品名、唯一 SKU `17-1366-24400899999`、税入价 1650 JPY、当前可售和主图 URL 与 master catalog 一致；目标价为 `ceil(1650 × 0.65)=1073` JPY，没有人民币换算。

最小 payload 以 `config/shijiu_native_create_shape_fixture.json` 的 54 个顶层字段、字段顺序、`spec_name` 与 `sku_info` 子字段为边界，只替换为 MIKIHOUSE 的真实商品名、固定类目 294884、DEFAULT 规格、一个真实 SKU、1073.00 JPY、库存 1、`state="1"`、`is_shelf=0` 和一张已上传 COS 的官方图片。fixture 只保存审计样例的字段/类型/值形态，不携带或提交 WAWU 商品内容与上游字段语义。

传输也改为与参考仓库 native fallback 一致：请求头为 `accept`、`content-type`、`referer`、`sec-ch-ua`、`sec-ch-ua-mobile`、`sec-ch-ua-platform` 和 Chrome 151 User-Agent（有配置时另带 cookie），不再发送旧 MIKI importer 的 `Origin` 或自定义 User-Agent；Content-Type 为 `application/json;charset=UTF-8`；body 采用 UTF-8、`ensure_ascii=false`、紧凑分隔符序列化，`secret`/`token` 先于 payload，token 同时进入 query。逐字段差异保存在 `minimal_create_payload_diff.json`。

目标 `/v1/cos/upload` 成功返回一张 `cdn0.19mini.com` 图片。唯一一次 `/shopapi/Goods/newAddGood` 返回 HTTP 200、JSON Content-Type、`code=200, msg=success, data=[]`。其后精确 SKU 和精确名称查询均未返回 ID，11 个 MikiHouse 分类过滤视图的完整分页仍为创建前同一组 286 个 ID。没有唯一 product ID，因而不存在可安全调用 `getFormatInfo` 的目标，也没有 SKU ID 或 mapping 可持久化。

本轮总请求 321：319 次只读、1 次图片上传、1 次最小创建；渐进 edit 0、批量处理 0、legacy 修改 0、`00-1000-028` 请求 0。一次性 checkpoint 为 `STOPPED_ON_PROBE_ERROR`，禁止重试。规格、完整轮播和详情三组更新门禁均未到达，所以现有结果不能把静默拒绝归因于这些扩展字段。已证明的是“native-shaped 请求被解析并返回空 success，但没有可观测的持久实体”；尚不能证明具体由目标端校验、账号权限、租户上下文或其他后台工作流条件造成，禁止选择第二个商品继续试写或猜测 ID。

## 会话、Cookie 与 browser-exact 证据审计

参考仓库 current main `a36c5eab40bf419562ba03d15c090151698d582a` 的 `backend_client.py` 将 `NATIVE_SAVE_REQUEST_PATH` 作为 Git 外模板来源。`_native_save_headers()` 读取模板 headers 后主动删除其中的 Cookie，只有本地 `MYSHOP_COOKIE` 非空时才注入 Cookie。当前外部 `shijiu.env` 有 token/secret、无 `MYSHOP_COOKIE/SHIJIU_COOKIE`，也没有自定义 native path；因此 MIKIHOUSE 上一轮请求不含 Cookie 与代码和运行配置一致。

历史 native 模板捕获于 2026-08-14，endpoint、54 个 payload 字段、JSON 顺序、Content-Type 和浏览器 header 子集均有效，且对应的原生 UI 保存曾由 Goods.index 确认可见。但捕获器调用 `request.headers()`，没有调用 `request.allHeaders()` 或监听 `Network.requestWillBeSentExtraInfo`；Playwright 的基础 headers 结果不足以证明 Cookie 等受保护 headers 是否真实存在。模板的无 Cookie 状态只能表述为“未观察到”，不能表述为“浏览器未发送”。

同时，历史 WAWU programmatic direct loop 的请求预览也不含 Cookie，却完成商品创建、SKU 回读校验和删除后确认。这一反证说明 Cookie 缺失不是当前静默拒绝的充分原因。现有 token/secret 仍能完成 MIKIHOUSE 的只读列表和分类扫描，说明读取认证有效，但创建授权、当期浏览器会话及租户工作流仍未得到 browser-exact 证据。

浏览器只读环境检查没有发现应用内 Shijiu 标签；Chrome 正在运行但无可连接的 ChatGPT 浏览器扩展，未读取 Cookie、storage 或密码。由于缺少当前登录会话、完整受保护请求头和当期成功保存/回读对，审计严格停止在 `BLOCKED_MISSING_BROWSER_EXACT_SESSION_EVIDENCE`。本轮没有发 Shijiu read/upload/create/update 请求，没有选择另一件商品，也没有修改 legacy 286。缺失私有证据和安全存放方式详见 `deliverables/shijiu_import/session_auth_audit.json`；所有 token、secret、Cookie、cURL/HAR 与原始 body 必须留在 Git 工作区之外。

## Browser-exact 捕获工具与当前门禁

新增的 `scripts/shijiu_browser_exact_capture.mjs` 是严格本地的证据工具，不是商品 importer。它以 Playwright `request.allHeaders()` 和 CDP `Network.requestWillBeSentExtraInfo` 双路径记录一次人工原生保存，并在同一已登录页面中只读执行 `Goods/index` 与 `getFormatInfo`，只有唯一回读得到 `product_id`/`sku_id` 才将证据标为 verified。脚本自身不填写、不点击、不创建；只有人工在浏览器中保存一次非 MIKIHOUSE、非类目 294884 的一次性测试商品。任何识别为 MIKIHOUSE 的 `newAddGood` payload 均在网络传输前中止。

原始 endpoint/query、完整 headers、Cookie/token/secret、body、响应及回读原文只允许写入 Git 工作区外的 `--private-dir`，目录/文件权限分别为 `0700`/`0600`。可提交的 `browser_exact_capture_readiness.json` 只包含字段名、类型、布尔存在性、哈希以及成功后可公开的目标 ID；私有路径本身也不写入报告。

2026-09-04 本机预检发现 Chrome 152 已运行，但没有 ChatGPT Chrome 扩展且未开放 `127.0.0.1:9222` CDP，因此无法附着当前默认会话，状态保持 `BLOCKED_EXISTING_CHROME_NOT_ATTACHABLE`。可行的最少路径是由脚本启动 Git 外的独立私有 Chrome profile 后人工登录并保存一次，或人工启动一个带本地 CDP 端口的专用非默认 profile；禁止复制默认 Chrome profile。当前历史 WAWU 与此前 MIKIHOUSE 的脱敏比较在 method、endpoint、query/header 名称、Content-Type、body/sku/spec 字段和类型上均未发现差异，故尚无证据支持修改认证或 payload。必须先取得当期成功 browser-exact 请求和唯一回读对，再根据自动差异报告决定下一步修复。
