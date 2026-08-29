# FlowSpec JSON 模型契约

只有在需要保存、校验、渲染或比较流程时读取本文件。对一次性的短回答，不必强制生成 JSON。

## 顶层字段

必填字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `version` | string | 当前建议为 `1.0` |
| `title` | string | 流程名称 |
| `diagram_type` | string | `flowchart` 或 `state` |
| `nodes` | array | 节点列表 |
| `edges` | array | 有向边列表 |

常用可选字段：

- `status`：`draft`、`review_ready` 或 `verified`；省略时按 `draft` 处理。`review_ready` 不允许开放的阻塞问题，`verified` 也不允许未验证假设。
- `direction`：`TD`、`TB`、`LR`、`RL` 或 `BT`；默认 `TD`。
- `scope`：包含 `goal`、`in_scope`、`out_of_scope`。
- `actors`：`{"id": "ACT-USER", "name": "用户"}`。
- `sources`：需求来源，至少包含 `id`、`kind`、`ref`。
- `facts`：包含 `id`、`statement`、`source_ids`。
- `assumptions`：包含 `id`、`statement`、`risk`、`verification`、`status`，可用 `source_ids` 指向形成该假设的材料。
- `questions`：包含 `id`、`question`、`blocking`、`owner`，可用 `source_ids` 指向暴露该缺口的材料。
- `acceptance_criteria`：Given/When/Then 验收标准。
- `test_scenarios`：可执行的完整路径测试。

## 节点

每个节点必须包含：

```json
{
  "id": "N-010",
  "type": "action",
  "label": "校验账号与密码"
}
```

`type` 可用值：

- `start`：唯一的合成开始节点；
- `action`：用户或系统动作；
- `decision`：可判定的分支问题；
- `external`：第三方或边界外系统调用；
- `state`：状态图中的业务状态；
- `end`：成功、失败、取消或其他明确终点。

建议字段：`actor_id`、`source_ids`、`fact_ids`、`assumption_ids`、`preconditions`、`inputs`、`outputs`、`observability`、`acceptance_ids`。

## 边

每条边必须包含 `id`、`from`、`to`：

```json
{
  "id": "E-020",
  "from": "N-010",
  "to": "N-020",
  "condition": "凭据有效",
  "kind": "normal"
}
```

`kind` 可用值：`normal`、`error`、`timeout`、`cancel`、`retry`、`fallback`。关键边可使用 `source_ids`、`fact_ids`、`assumption_ids` 保留证据关系。决策节点的每条出边都要有不同的 `condition`；无法穷举时，把一条边设为 `"is_default": true`。重试边应提供 `limit` 或 `stop_policy`。

状态流转还可使用 `event`、`guard`、`effect`。渲染标签按 `event [guard] / effect` 组合。

## 验收标准与测试

验收标准示例：

```json
{
  "id": "AC-010",
  "node_ids": ["N-010", "N-020"],
  "edge_ids": ["E-010"],
  "given": "账号存在且未锁定",
  "when": "用户提交正确凭据",
  "then": ["创建登录会话", "登录失败计数清零"],
  "must_not": ["返回其他账号的数据", "把密码明文写入日志"],
  "verification": "接口响应、会话记录与审计日志一致",
  "priority": "required"
}
```

测试场景示例：

```json
{
  "id": "TC-010",
  "title": "正确凭据登录成功",
  "category": "happy",
  "path": ["N-START", "N-010", "N-020", "N-END-OK"],
  "covers": ["AC-010"],
  "preconditions": ["账号存在且未锁定"],
  "steps": ["提交正确账号和密码"],
  "expected": ["返回成功", "创建会话"],
  "postconditions": ["失败计数为 0"],
  "observability": ["login_result=success"]
}
```

`category` 建议使用 `happy`、`edge`、`error`、`state`、`recovery`。每条测试必须是一条从入口到明确终点的完整路径，不能只测试孤立节点。

## ID 和追踪规则

- ID 在同一命名空间内唯一且版本间保持稳定；修改显示文本不要顺手重编号。
- 推荐前缀：`SRC-`、`F-`、`H-`、`Q-`、`ACT-`、`N-`、`E-`、`AC-`、`TC-`。
- `source_ids` 只引用 `sources`；`fact_ids` 只引用 `facts`；`assumption_ids` 只引用 `assumptions`。
- 验收标准通过 `node_ids` 和 `edge_ids` 关联节点与关键分支；测试通过 `covers` 关联验收标准，并用 `path` 声明完整节点路径。
- `review_ready` 候选应运行 `validate --strict`；`verified` 只标记已有证据覆盖的范围，不能用结构校验代替真实接口、设备、人工或生产证据。
- 变更前后保持 ID 稳定后，可运行 `flowspec.py diff`；脚本会把节点和边的变化传播到直接关联的验收标准、测试、假设及日志字段。它不会自动推断接口兼容、数据迁移或业务语义影响，这些仍需人工复核。

## 渲染格式

- `render <spec.json>`：默认在输入 JSON 旁直接生成同名 `.drawio`，不需要指定格式或输出路径。
- `--output <flow.drawio>`：指定图文件路径；扩展名必须为 `.drawio`。
- `--force`：确认替换已有输出文件；未指定时保留原文件并返回错误。
- `--format markdown`：输出完整文本评审文档；它不是独立图文件格式。需要图形交付时始终生成 `.drawio`。

draw.io/XML 和 Markdown 都来自同一份 FlowSpec JSON。Agent 仍可把现有 Mermaid 文本作为审计输入。任何格式可打开或可读取，只能证明产物结构与序列化正常，不能证明业务规则真实。

## 参考样例

完整格式见 [login-security.example.json](login-security.example.json)。它只是结构示例；其中阈值和规则均标记为待验证，不能复用为真实业务事实。
