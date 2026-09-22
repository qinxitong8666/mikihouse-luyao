# luyao-quote-assistant 只读参考审计

- 参考仓库：`qinxitong8666/luyao-quote-assistant`
- 只读检出 commit：`527e7e38cf7bd76c8f29a8701af96e4f76d5ae7d`
- 本次用途：仅学习通用微信收藏 Sink、Mac GUI/双击入口与安全边界
- 明确未复用：商品来源、商品匹配、分类、定价、过滤及 manifest 业务语义

## 观察到的通用模式

1. `production_quote_runner.py` 的 `WeChatFavoriteSink` 消费已经冻结的最终 manifest，而不是在 Sink 内重新计算商品业务结果；本仓库同样让 PDF、文字和 preview 只消费 `DailyQuoteManifest`。
2. Mac GUI 把“生成/预览”和具有副作用的收藏写入分层；未经运行时验证的保存能力应保持禁用。本仓库当前 GUI 只提供生成与两个 preview。
3. 微信页面识别需要强页面指纹，不能把聊天页中的“发送收藏”误当成收藏页，也不能依赖失效坐标。本仓库已复用强指纹、分段写入和强回读机制，但没有复制任何商品或报价逻辑。
4. 参考配置中的 `wechat_note_length_limit` 为 `null`，没有硬容量契约。
5. 历史 evidence 曾把约 27,893 字符拆成 5 段写入同一笔记，并得到累计文本 hash 回读一致；但最终状态仍是 `PHASE_2B3_WAITING_MANUAL_EDITOR_CHECK`，不足以证明正式保存后的一条微信收藏容量，更不足以覆盖本次 104,006 字符样例。

## 本仓库结论

- 真实 Mac/微信验收已执行，PDF 附件收藏 PASS；
- 文字报价仅证明到 30,000 字符的保存重开，104,006 依规则未测试，所以整体仍 fail closed；
- 文字报价完整保留为单文件和单条 preview，不删商品、不擅自拆成第三或第四条收藏；
- 正式微信收藏写入仍由独立双重开关禁用，直到完整文字容量获得新的明确授权与验收。
