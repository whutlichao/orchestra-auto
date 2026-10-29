# 散文契约：六条不得漂移的判据

**什么时候读**：验收时、审查任一语言包的改动时，或两份文档对「从某个信号能推出什么结论」看似不一致时。

本 skill 其余部分是流程。这六条判据是流程所依赖的承重结论：每条都点名一个文档**拒绝**下的结论，
因为另一种看似合理的读法会毁掉一个 Run。它们汇总在一页，是为了让 reviewer——或运行中的
coordinator——能在一处对照全部六条，而不必从各份 reference 里拼回来。

## 六条判据

1. **`absent` 不是没发生的证明。** `request-show` 回 `absent` 只说明权威不认识这个 request 键，
   并不说明动作没跑过。只有权威侧的正面缺失——该 Task 没有 Dispatch、该 head 没有 PR——才允许判定
   「未执行」。判定前先盘点 Orca 与 git。见[协调台账](ledger.md)。
2. **看板故障绝不阻断派发。** 进度记录、注册表与浏览器只是 sidecar。写入失败、锁超时、旧服务不可达、
   或 viewer 起不来，都只记台账 gap 并告警，派发走同一条路继续；`open` 从不在派发的关键路径上。
   见[进度记录与看板](progress-dashboard.md)。
3. **`worker_done` 不是验收。** 它只证明 worker 已结算，仅此而已。验收要可观察结果与检查证据；
   `grep` 只证明字符串存在，健康状态从不授权 release。
4. **仅结算且有收尾记录后才 release。** release 需要已结算的 Dispatch 和一条持久收尾记录：成功时至少
   有验收记录与 dispatch → task → attempt → worktree 映射，失败时至少有失败分类与证据引用。
   无论健康状态说什么，都绝不 release 未结算的 worker。
5. **`ready` 不是已启动轮次。** `state: ready` 只表示 Dispatch 就绪，不表示 agent 的 turn 已开始。
   `turnStart: observed` 才是回执的启动证据；当它是 `unsupported`、`unobserved` 或缺失时，
   按该 agent 支持的面取证。
6. **`unknown` 不是失败。** 证据缺失、权威读不到、TTL 过期或字段缺失都是 `unknown`：下一步是取证，
   不是重试动作。空等待不等于空队列，未答复也不等于拒绝。

## 这一页不是

钉住这些锚点的测试**只查锚点，不等于完整语义等价**：它位于 `tests/test_package_content.py`，
在两个语言包里逐条找这些短语。那是清单，**不是翻译**：译者可以保留全部锚点，同时让锚点之间的段落
悄悄漂移，所以 reviewer 仍要读 diff。这六条也不证明周边流程完整——只证明上面这些结论在两个语言包里
并排发布。
