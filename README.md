# 平台能力门禁 (astrbot_plugin_platformgate)

按**平台**白名单控制哪些插件的**指令**和 **LLM 工具**可用。

默认全部拦截：在 AstrBot 的 WebUI 里勾选后，才允许该插件在对应平台执行指令和调用工具。

## 为什么需要它

某些插件（如群分析、QQ空间等）在 **OneBot11（napcat）** 上运行容易导致 QQ 账号封禁。
本插件从源头把它们从易封号平台的白名单里排除——既不响应它们的指令，也不让 LLM 调用它们的工具。

- `aiocqhttp` = napcat / OneBot11（高风险）
- `qq_official` = QQ 官方机器人

## 功能

- **WebUI 可视化管理**：列出所有已注册插件及其指令、LLM 工具，每个平台一个白名单开关
- **指令拦截**：命中未放行插件的指令 → 立即终止事件传播（`event.stop_event()`）
- **LLM 工具拦截**：未放行插件的工具被调用时返回"该平台不可用"，不执行真实逻辑
- **按平台隔离**：napcat 和官方机器人互相独立配置
- **持久化**：规则存到 `data/plugin_data/astrbot_plugin_platformgate/rules.json`

## 配置项（_conf_schema.json）

| 配置 | 默认 | 说明 |
|------|------|------|
| `enabled` | true | 总开关 |
| `default_allow_all` | false | 默认策略：false=默认全拦(白名单)，true=默认全放(黑名单) |
| `block_hint` | false | 拦截时是否发提示（易封号平台建议关，静默） |
| `block_hint_text` | "" | 提示文案，`{plugin}`/`{command}` 占位 |
| `protect_own_plugin` | true | 始终放行本插件自身 |
| `debug_log` | false | 打印每条约拦截/放行日志 |

## 安装

1. 在 AstrBot 管理面板安装本插件（或把本目录放入 `addons` / 插件目录）
2. 打开插件 WebUI（"平台能力门禁"）
3. 选择平台 → 勾选要在该平台放行的插件 → 保存即生效

## 工作原理（均经 AstrBot 源码确认）

- 枚举插件：`astrbot.core.star.star.star_registry / star_map`
- 枚举指令：`star_handlers_registry` 中 `CommandFilter.get_complete_command_names()`
- 枚举 LLM 工具：`ctx.get_llm_tool_manager()` 的 `func_list`，用 `handler_module_path` 反查插件
- 指令拦截：`@filter.event_message_type(ALL, priority=100)` + `event.stop_event()`
- 工具拦截：`@filter.on_using_llm_tool()` 动态包装 handler，被拒时返回提示

## 提示

- 被拦截的指令/工具对**管理员**放行（`event.is_admin()`）
- 拦截默认**静默**（推荐，防封号场景）——需要用户看到才开 `block_hint`
