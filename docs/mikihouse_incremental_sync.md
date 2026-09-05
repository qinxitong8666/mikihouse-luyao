# MIKIHOUSE 稳定池增量同步契约

## 边界

`output/storefront-stable/stable_catalog.json` 是唯一正式 source of truth。增量引擎可以读取完整官网 source snapshot 来判断可见性与稳定性转换，但只有同时出现于当期 `stable_catalog` 的商品才能产生新建、调价、库存或图片更新语义。351个 `PDF_SPECIAL_LIST` 品番及已逐项证明的 `NON_SELLABLE_SERVICE_OR_ADDON` 永久清单在事件与动作层都再次前置拦截；价格为0不能单独构成非销售项证据。

本轮终止点固定为 `PLANNING_ONLY`。实现中不导入 Shijiu client，也不存在任何执行 action plan 的参数。WAWU 可能在同一正式租户写入时，不请求 writer mutex，不访问 Shijiu/COS。

## 双轴状态机

状态不将“官网消失”与“稳定性隔离”合并：

- source presence：`ACTIVE` / `INACTIVE`，只有完整分页成功的全站 crawl 才能把对象改为 `INACTIVE`；
- stability：`STABLE` / `WEB_EXCLUSIVE` / `LIMITED_TIME_PRICE` / `REVIEW_REQUIRED_STABILITY` / `PDF_SPECIAL_LIST` / `NON_SELLABLE_SERVICE_OR_ADDON`；
- 稳定商品进入不稳定状态时，只对已有 MIKIHOUSE ownership mapping 的商品产生 `STABILITY_QUARANTINE`，未来语义是整商品临时下架并保留 mapping；
- 隔离商品恢复正常状态时，正常税入 JPY 先重算 `ceil(price×0.65)` 并通过 price guard，才产生 `STABILITY_RESTORED`；
- 新的 WEB 限定、限时促销、待复核或非独立销售商品不产生 `CREATE_PRODUCT`；
- 促销价只能触发隔离，不能产生 `PRICE_CHANGED`。

官网可见性恢复使用 `PRODUCT_REACTIVATED` / `VARIANT_REACTIVATED`；稳定性恢复使用 `STABILITY_RESTORED`。两者原因独立保存，不得相互冒充。

## 事件、价格与库存

规范化事件是：`NEW_PRODUCT`、`NEW_VARIANT`、`PRICE_CHANGED`、`INVENTORY_CHANGED`、`IMAGE_CHANGED`、`PRODUCT_INACTIVE`、`VARIANT_INACTIVE`、`PRODUCT_REACTIVATED`、`VARIANT_REACTIVATED`、`STABILITY_QUARANTINE`、`STABILITY_RESTORED`、`NO_CHANGE`、`REVIEW_REQUIRED`。

价格全程是 JPY，不做人民币换算。超出 `config/shijiu_price_guard.json` 绝对值或比例阈值的变动只生成 `REVIEW_REQUIRED`。库存仅使用 Storefront `availableForSale`：`true -> 1`、`false -> 0`，不构造官网没有提供的件数。

事件 ID 由类型、精确 product/variant identity 和转换内容规范化哈希得到。`state/mikihouse_source_sync_state.json.gz` 持久保存最后一次完整 crawl、所有 source 对象、event ledger、待处理与已消费事件。只有未消费事件会保留在后续 action plan；消费必须发生在未来受权写入并完成强回读之后。

## 统一周期入口

手动与定时任务只改变 trigger 标签，共用同一实现：

```bash
# 对已完成官网快照做纯离线规划
PYTHONPATH=src .venv/bin/python scripts/sync_mikihouse_cycle.py --trigger manual

# 定时任务的同一入口，先做全站只读抓取
PYTHONPATH=src .venv/bin/python scripts/sync_mikihouse_cycle.py \
  --trigger scheduled --refresh-storefront
```

周期固定为：完整官网快照→重建稳定/排除池→比较 `last_successful_state`→生成事件→生成不可执行的 Shijiu action plan→`PLANNING_ONLY` 停止。不完整 crawl 在更新状态文件前失败关闭。

本地运行产物位于 `output/storefront-sync-cycle/`；Git 只保留压缩 source state 与 `deliverables/storefront_stable_catalog/` 中的脱敏汇总。
