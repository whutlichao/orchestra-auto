# 实施计划维护

本目录保存本地实施计划，不代替 GitHub issue tracker，也不表示已发布或批准远端票图。

- [工作流重构计划](skill-workflow-plan.md)：当前计划的入口与依赖表。
- [分析](analysis/skill-workflow.md)：模块与跨模块集成关系。
- `tasks/`：每个独立交付单元的范围和验证；执行前从批准基线创建隔离分支。
- `reviews/`：独立审阅记录，区分规划审阅与后续实现验证。
- [backlog](backlog.md)：未纳入当前范围的改善。

状态规则：pending 为前置未完成；ready 为依赖已验收；in-progress 为正在执行；blocked 记录实际阻塞；done 必须附产物、验证及独立审阅证据。编写任务文件不会使任务自动完成。

只有用户选择实际实施后才发布 tracker、派实施 worker。若走 `to-tickets`，先核对粒度及阻塞，再按仓库约定发布，不直接把本地计划标为已批准票图。执行端点以用户授权为准；计划产出不授权推送或合并。
