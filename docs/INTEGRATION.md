# 接入与验证

操作范围必须来自真实观测与明确目标。一个 profile 包含 `manifest.json`、私有 `credentials.json`、生成的 Python 客户端、MCP 入口及后续验证状态。整个 profile 都应保留在 Git 忽略的本地目录。

## 1. 采集与发现

可以导入 HAR 1.2，或通过 `explore-start`、`explore-round`、`explore-propose` 采集与起草。交互式探索需要先运行 `ego-browser skill`，按当前浏览器版本的说明操作。

```sh
python -m web2mcp explore-start work/session \
  --url https://example.com --goal '读取任务列表与详情' --budget 40
printf '%s\n' '{"kind":"observe"}' > work/observe.json
python -m web2mcp explore-round work/session --action-file work/observe.json
python -m web2mcp explore-propose work/session
```

浏览器步骤应基于最新页面观察，包含动作目的和显式 `reviewed` 标记；这不是对业务提交的通用授权。密码和验证码由用户手动输入。发生中断后，先核对页面与远端状态，不重复发送不确定的写操作。

探索生成的候选默认内部可见，需要试验后发布。低层 `discover` 是开发者入口，直接生成原始操作；不要把其所有候选不经审查地挂给 AI。

## 2. 验证读取

`trial-read` 的每个 case 都需要结果断言。有参数的操作需要至少两个不同参数的 case。例如，已观察并命名为 `get_task` 的操作：

```json
[
  {"arguments":{"path":{"task_id":"101"}},"assertions":[{"pointer":"/data/id","value":"101"}]},
  {"arguments":{"path":{"task_id":"102"}},"assertions":[{"pointer":"/data/id","value":"102"}]}
]
```

把示例按真实接口结构改好并保存在 `work/cases.json` 后：

```sh
python -m web2mcp trial-read work/site-profile get_task work/cases.json --reviewed-read-only
python -m web2mcp publish-reads work/site-profile get_task
```

断言应验证业务目标和记录身份，不能只验证 HTTP 200。修改操作定义会使旧试验证据失效。

## 3. JSON 接入包

```sh
python -m web2mcp draft-pack work/site-profile --name my_product --out work/product.pack.json
# 按真实表单、请求和响应补充读取步骤、输出映射与契约。
python -m web2mcp check-pack work/site-profile work/product.pack.json
python -m web2mcp trial-pack work/site-profile work/product.pack.json
python -m web2mcp install-pack work/site-profile work/product.pack.json --reviewed
```

参考 `examples/packs/` 中的 REST、RPC、表单三种合成站点示例，以及 [完整语法](../web2mcp/pack_reference.md)。接入包描述接口参数和工作流，不需要为每个表单生成新的 Python 类。

## 4. 写入契约

契约定义输入 schema、预检读取、绑定记录状态、允许写入字段、持久化锁和提交后的回读断言。参见 `examples/purchase_contract.json`。

```sh
python -m web2mcp install-contract work/site-profile work/reviewed-contract.json --reviewed
```

安装后通过契约的 `*_prepare` 取得预览；确认该次业务决定后调用 `*_submit`，按 `*_status` 核对已尝试的操作。预览 ID、远端记录版本和允许字段都是约束的一部分，不能修改预览后继续使用旧的批准。

## 5. 登录接力

先在 profile 的 `runtime.json` 中配置只读身份探针，值来自真实观察：

```json
{"identity_probe":{"operation":"me","pointer":"/data/id","expected":"example-user"}}
```

重新登录后，采集同一账号、同一接口与所需认证来源：

```sh
python -m web2mcp refresh-login work/site-profile work/fresh.har
# 对已审定的显式浏览器存储来源，也可使用：
python -m web2mcp refresh-storage work/site-profile work/storage.json
```

刷新校验固定身份和已审定的来源，不改变业务操作定义。未观测到或未匹配到的端点不会被假装刷新成功。重新启动使用该 profile 的 MCP 进程以读取新凭据。

## 6. 运行维护

配置身份探针后会提供 `runtime_health`、`runtime_cards`、`runtime_audit_verify`。本地审计哈希链用于发现意外篡改，不构成外部可信签名。诊断清理：

```sh
python -m web2mcp purge-diagnostics work/site-profile --days 7
```

涉及敏感正文时，在 `manifest.json` 启用 `sensitive_responses`，并按需要设置 `contract_privacy: "metadata_only"`。这不会自动清除过去已有的捕获或诊断文件；历史文件仍须由本地使用者管理。
