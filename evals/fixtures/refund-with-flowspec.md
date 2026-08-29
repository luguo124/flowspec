# 退款申请与支付渠道异常处理（结构示例）

- 状态：Draft（草案）
- 证据边界：结构校验与可重复渲染不等于业务规则真实。

## 范围与目标

- 目标：把退款申请、审核、渠道退款和异常转人工串成可追踪流程
- 范围内：退款申请；资格判断；人工审核；支付渠道退款；超时查询；人工处理
- 范围外：具体退款时限；部分退款计算；支付渠道协议细节

## 证据、假设与问题

### 已确认事实

- **F-001**：用户可以提交退款申请（来源：SRC-001）
- **F-002**：审核通过后发起退款（来源：SRC-001）
- **F-003**：支付渠道异常需要进入人工处理（来源：SRC-001）

### 待验证假设

- **H-001**：每次退款请求都有可复用的幂等标识
  - 风险：超时后的重复请求可能造成重复退款
  - 验证：核对订单系统与支付渠道的幂等契约
  - 状态：unverified
- **H-002**：支付渠道支持按退款请求查询最终状态
  - 风险：无法查询时不能安全判断超时请求是否已经成功
  - 验证：核对支付渠道查询接口、状态集合与一致性时限
  - 状态：unverified

### 阻塞问题

- **Q-001**：哪些订单状态、商品类型和申请时间满足退款资格？（owner：产品/业务）
- **Q-002**：退款金额如何计算，是否允许部分退款？（owner：产品/财务）

### 非阻塞问题

- **Q-003**：人工处理的负责人、时限和用户通知方式是什么？（owner：运营/客服）

## draw.io 文件

- 独立图文件由同一 FlowSpec JSON 执行 `render` 生成；默认扩展名为 `.drawio`。
- Markdown 评审与图文件共享同一份来源、稳定 ID 和追踪关系。

## 逻辑审计

- **WARNING · blocking_question_open**：哪些订单状态、商品类型和申请时间满足退款资格？（Q-001）
- **WARNING · blocking_question_open**：退款金额如何计算，是否允许部分退款？（Q-002）

## 验收标准

### AC-001

- 关联节点：N-SUBMIT；N-ELIGIBLE
- 关联边：E-001；E-002；E-003；E-004
- Given：用户提交退款申请
- When：订单系统判断退款资格
- Then：满足资格时进入审核；不满足时给出明确结果
- 不得发生：在资格规则未确认前编造退款时限
- 验证证据：申请记录、资格判断结果和用户响应一致
- 优先级：required

### AC-002

- 关联节点：N-REVIEW；N-APPROVED
- 关联边：E-005；E-006；E-007
- Given：退款申请满足资格并进入审核
- When：审核人员提交决定
- Then：通过时发起渠道退款；拒绝时结束申请并保留原因
- 不得发生：审核拒绝后仍发起退款
- 验证证据：审核记录、退款请求和申请状态一致
- 优先级：required

### AC-003

- 关联节点：N-REFUND；N-RESULT；N-RECORD-SUCCESS；N-RECORD-FAIL
- 关联边：E-008；E-011；E-012；E-018；E-019
- Given：退款审核通过
- When：支付渠道返回明确结果
- Then：成功时记录成功并通知用户；失败时记录渠道失败信息
- 不得发生：同一退款请求重复入账；渠道失败时标记退款成功
- 验证证据：订单退款记录、渠道结果、账务结果和通知记录一致
- 优先级：required

### AC-004

- 关联节点：N-REFUND；N-QUERY；N-QUERY-RESULT；N-MANUAL；N-RECORD-SUCCESS
- 关联边：E-009；E-010；E-013；E-014；E-015；E-016；E-017；E-020
- Given：渠道退款请求超时、调用异常或结果未知
- When：系统查询最终状态或创建人工任务
- Then：确认成功后只记录一次成功；无法确认时进入人工处理并保留关联标识
- 不得发生：超时后直接重发导致重复退款；结果未知时向用户宣称退款成功
- 验证证据：退款请求、查询记录、人工任务和最终账务状态可关联
- 优先级：required

## 测试场景

### TC-001 · 审核通过且渠道退款成功

- 类别：happy
- 完整路径：N-START；N-SUBMIT；N-ELIGIBLE；N-REVIEW；N-APPROVED；N-REFUND；N-RESULT；N-RECORD-SUCCESS；N-END-SUCCESS
- 覆盖验收：AC-001；AC-002；AC-003
- 前置条件：申请满足待确认的退款资格
- 操作步骤：提交申请；审核通过；支付渠道返回成功
- 预期结果：退款只记录一次成功；用户收到结果
- 结束状态：退款状态为成功
- 可观测证据：refund_status=success

### TC-002 · 申请不满足退款资格

- 类别：edge
- 完整路径：N-START；N-SUBMIT；N-ELIGIBLE；N-END-INELIGIBLE
- 覆盖验收：AC-001
- 前置条件：申请不满足待确认的退款资格
- 操作步骤：提交申请
- 预期结果：返回不满足资格的结果；不进入审核
- 结束状态：未发起渠道退款
- 可观测证据：refund_request_created=false

### TC-003 · 审核拒绝

- 类别：error
- 完整路径：N-START；N-SUBMIT；N-ELIGIBLE；N-REVIEW；N-APPROVED；N-END-REJECTED
- 覆盖验收：AC-001；AC-002
- 前置条件：申请满足资格
- 操作步骤：提交申请；审核拒绝
- 预期结果：保存拒绝结果；不发起渠道退款
- 结束状态：申请结束
- 可观测证据：review_result=rejected

### TC-004 · 退款请求超时后查询确认成功

- 类别：recovery
- 完整路径：N-START；N-SUBMIT；N-ELIGIBLE；N-REVIEW；N-APPROVED；N-REFUND；N-QUERY；N-QUERY-RESULT；N-RECORD-SUCCESS；N-END-SUCCESS
- 覆盖验收：AC-001；AC-002；AC-004
- 前置条件：审核通过；渠道请求超时；渠道查询可用
- 操作步骤：发起退款；按原请求标识查询；查询确认成功
- 预期结果：不重复发起退款；只记录一次成功
- 结束状态：退款状态为成功
- 可观测证据：refund_request_id；query_result=success

### TC-005 · 支付渠道调用异常转人工

- 类别：recovery
- 完整路径：N-START；N-SUBMIT；N-ELIGIBLE；N-REVIEW；N-APPROVED；N-REFUND；N-MANUAL；N-END-MANUAL
- 覆盖验收：AC-001；AC-002；AC-004
- 前置条件：审核通过；渠道调用异常
- 操作步骤：发起退款；记录异常并创建人工任务
- 预期结果：人工任务关联原退款请求；不宣称退款成功
- 结束状态：申请等待人工处理
- 可观测证据：manual_case_id；exception_reason

## 追踪矩阵

| 需求来源 | 节点 | 边 | 验收标准 | 测试场景 |
| --- | --- | --- | --- | --- |
| SRC-001 | N-START；N-SUBMIT；N-ELIGIBLE；N-REVIEW；N-APPROVED；N-REFUND；N-RESULT；N-RECORD-SUCCESS；N-RECORD-FAIL；N-MANUAL | E-001；E-002；E-003；E-004；E-005；E-006；E-007；E-008；E-009；E-010；E-011；E-012；E-014；E-015；E-017；E-018；E-019；E-020 | AC-001；AC-002；AC-003；AC-004 | TC-001；TC-002；TC-003；TC-004；TC-005 |
