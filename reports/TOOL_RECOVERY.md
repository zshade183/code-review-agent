# 工具失败与输出恢复

工具统一通过 `Tool.__call__` 校验参数、执行和校验结果，失败携带
`error_type`、`retryable`、`attempts`，通过 Observation 反馈给模型。
错误分为参数错误、临时错误、权限错误、结果格式错误及其他执行错误。

只有显式声明 `retry_safe=True` 且无副作用的工具自动重试临时错误。
默认最多执行两次，硬上限三次，退避等待计入工具总耗时。项目搜索、
文件读取及项目符号检索启用此策略；修改类工具不会自动重试。
目前识别标准库超时、连接错误和子进程超时；工具也可以显式返回
`retryable=True`。底层吞掉的异常不会凭错误文本猜测为可重试。
调用预算仍按模型请求计数，实际执行次数在轨迹 `attempts` 中单独记录。
本机制不强制中断任意阻塞函数；具体 I/O 的超时仍由工具自身负责。

证据缓存只复用成功结果，并保留结构化 data。失败仍保留在证据记录中，
但下次相同调用会重新执行。失败证据不进入 successful_ids。
缓存命中时 attempts=0，表示本次没有执行底层工具。

主要审查工具声明 result_fields，检查 data 的必需字段与类型；其他工具
可覆盖 validate_result 实现更深入的业务约束。当前不是递归 JSON Schema 验证。

Review/Verification 的最终输出必须包含 issues/reviews 对象列表。
非法 JSON、缺少字段或列表类型错误最多进行一次不绑定工具的格式修复；
失败后由工作流记录 error，不把非法字段默认为空列表。格式修复是额外一次
模型调用；问题级字段和证据仍由后续原有校验处理。

回归验证：

```powershell
.venv/Scripts/python.exe -m unittest discover -s test -p test_tool_recovery.py
.venv/Scripts/python.exe test/test_langgraph_agent.py
.venv/Scripts/python.exe test/test_agent.py
```
