# MaiBot Per User Persona

同一个 QQ 机器人、单个 MaiBot 实例，按私聊联系人使用独立人格和聊天存储。

**v0.2.3 · 社区插件 · GPL-3.0-or-later · 默认禁用**

原有联系人继续走麦麦原流程；新增联系人走独立文本流程。每人可以设置角色名、背景、性格和回复风格，并可选启用现有表情包。无需修改 QQ 昵称或头像，也不创建第二个 MaiBot 实例。

本项目并非“首个人格切换插件”。它侧重联系人路由、独立 SQLite 和对共享记忆流程的隔离。当前是从实际部署整理的**限定环境源码版**，不是适配所有 MaiBot 版本的一键安装包。

## 兼容范围：安装前先看

| 项目 | 当前支持与限制 |
| --- | --- |
| MaiBot | **1.3.15**；清单限定此版本，其他版本需要重新审查调用链 |
| Plugin SDK | **2.16.0**，使用插件钩子、私有 API 与自定义 WebUI 页面 |
| Python | 3.12+；主验证环境为 Linux / Python 3.13 |
| 原适配器 | `MaiBot-SnowLuma-Adapter`，目录须为 `plugins/MaiBot-SnowLuma-Adapter` |
| OneBot | 当前参考实现限定 NapCat、`host.docker.internal:18080`、已配置 token；启动后核验机器人 QQ |
| 模型 | 读取原 `model_config.toml` 中唯一的 replyer 模型，限定 **DeepSeek 官方 API** |
| 聊天范围 | 已有好友的纯文本私聊；要求原部署关闭群聊，本插件也拒绝 QQ 群消息 |
| 表情包 | 可选，依赖 `Maibot_Emoji_Select_With_Text` 1.1.1 的已审查辅助文件及现有素材库 |

不同端口、其他适配器、多模型 replyer、第三方中转、不同插件 SDK、群聊混用等场景会拒绝或不在验证范围内。请先提兼容性需求，不要删除安全检查来强行启动。

## 能做什么

- 按 OneBot 事件中的真实用户 ID 路由，不靠昵称、文本或模型猜测。
- 每个 `(机器人 QQ, 联系人 QQ)` 一份独立 SQLite，独立上下文、历史检索、发送状态、配图偏好与冷却。同一联系人顺序处理，不同联系人可并发。
- 原管理员和原联系人不注入新人格，也不迁移、清空其历史。
- 新联系人不接入麦麦的表达学习、共享长期记忆、摘要、自动记忆通知、工具、天气、主动聊天或语音流程。
- 管理员登录后可在独立 WebUI 页面查看聊天文字、模型状态、配图描述和脱敏错误；不把新增记录写回麦麦共享聊天库。
- 可复用现有表情包进行发送，文字先发，没合适图片或选图失败时不随机补图。图片发送超时不自动重试。

**独立 SQLite 是应用层隔离，不是独立进程或操作系统沙箱。** 同机管理员、备份、恶意插件和模型服务商仍有其访问边界。详见 [SECURITY.md](SECURITY.md)。管理员查看独立聊天记录是明确支持的功能。

## 安装与配置

以下针对已有、已备份且符合上述条件的私聊部署。先备份原配置、数据和插件目录。插件不会替你修改核心白名单、适配器配置、QQ 账号资料或旧聊天数据。

### 1. 获取源码

在现有 MaiBot 项目根目录执行：

```sh
git clone https://github.com/KuriyamaMirai08/MaiBot-Per-User-Persona.git plugins/per-user-persona
```

也可[下载源码 ZIP](https://github.com/KuriyamaMirai08/MaiBot-Per-User-Persona/archive/refs/heads/main.zip)，解压并将仓库目录命名为 `per-user-persona`，放入现有 `plugins/`。本插件默认禁用，不会自动开放联系人。

### 2. 保持两个白名单分开

原 `config/adapter_policy.toml` 和原适配器 `filters` 继续只允许原联系人，且原核心私聊默认拒绝、群聊关闭、`ignore_self_message=true`。**不要把新增联系人加进原麦麦白名单。** 新联系人只加入本插件自己的 `authorized_user_ids`。

需要一致的原私聊白名单和适配器过滤配置；配置工具会计算它们的校验值。若这些配置之后变更，新人格会保守暂停，需人工审查后重新生成配置。

新增联系人应是尚未进入原共享记忆流程的好友。已存在原生聊天流时配置工具会拒绝；删除聊天流并不能证明旧长期记忆不存在。本版不提供共享历史迁移或清理工具。

### 3. 准备私人角色文件

复制 [personas.example.json](personas.example.json) 到仓库外的私人位置，例如项目的 `config/personas.private.json`。将示例 `20001` 替换成真实联系人 QQ，并设置角色内容及 `enabled: true`。所有 QQ 字段均使用字符串。

示例是虚构角色，**没有任何默认获准联系人**。可添加多个角色；不要把原联系人写进此映射。实际角色文件、密钥、聊天库都不要提交到 GitHub。

### 4. 只读预检，然后写入本插件配置

使用运行 MaiBot 的同一用户及其 Python 环境，在 MaiBot 项目根目录执行，确保限制权限后的私人配置仍能被服务读取。若是 Docker 部署，应在现有 MaiBot 容器内使用它实际看到的项目路径和 Python 环境；不要新增容器。

```sh
.venv/bin/python plugins/per-user-persona/configure.py \
  --root "$PWD" \
  --profiles config/personas.private.json \
  --authorize "替换为第二联系人的QQ号" \
  --unlock
```

不带 `--write` 时只读检查，不联网、不发消息、不写配置。`--authorize` 可重复指定。输出 `valid: true` 后，在同一命令末尾追加 `--write`，会生成本插件的 `config.toml`、`personas.json`、`reply_policy.json`、`emoji_policy.json`。

**生成后插件仍然禁用。** `--unlock` 只解除第二路由的配置锁，不会启用插件，也不改原白名单。到 MaiBot 后台确认配置后手动启用本插件，让联系人主动发“你好”验收；无需先由机器人给真人发送测试消息。

失败只输出脱敏错误类型。排查时核对兼容表、角色 `enabled`、旧聊天流、原白名单一致性和既有配置；不要把密钥或私人角色文件贴到公开 Issue。

已有本插件配置时，工具默认拒绝覆盖。确需重新生成时先停用插件，再使用 `--replace --write`；旧配置备份在 `data/plugins/local.per-user-persona/config-backups/`。这会重写四份插件配置并恢复默认回复策略，需人工合并自定义项；不会迁移或删除聊天数据库。

### 5. 可选：启用表情包

先安装原 [Maibot_Emoji_Select_With_Text](https://github.com/TogawaSakiko-desuwa/Maibot_Emoji_Select_With_Text)，并确认已有可用素材。运行目录须为 `plugins/togawasakiko-desuwa_emoji-select-with-text`。

初次生成配置时，在上面的命令追加 `--emoji-for "同一个获准联系人的QQ号"`。它会核对三个辅助文件的固定哈希；缺失或版本不同则拒绝。**不要盲目更新哈希来跳过兼容审查。** 默认不开启配图。

运行时只读使用官方 `emoji.get_all` 素材接口，复用候选整理、选图提示词和编号解析；不调用会读取原聊天流的 Tool，也不走原生发送后的自动记忆链。

共用的是表情图片和描述：启用就意味着这些素材可以发给新增联系人，请先检查素材库内容。独立记忆不等于素材库私有化。

用法：

- “发个开心的表情包”：明确索图，直接尝试匹配，不走日常概率门槛。
- 日常回复默认 20% 概率尝试配图、自动冷却 90 秒，每回合最多一张；没合适的仍可不发。
- “以后不要发表情包”：关闭该联系人的自动配图；之后明确索图可重新启用。
- 仅支持**发送**已有图片，不支持接收图片识别、收图学习或自动下载新素材。

选图单独使用 JSON Output 与非思考模式，避免正文为空导致 JSON 解析失败；异常最多重新生成一次，图片发送本身不自动重试。会额外消耗选图模型调用。

## 后台查看与数据位置

登录现有 MaiBot WebUI 后，在扩展页面打开“独立人格聊天记录”，路由为：

```text
/extensions/local.per-user-persona/history
```

页面要求管理员认证，只读且每页有限条数。不存在匿名聊天记录接口，也不展示图片 base64。原生“麦麦聊天 / 观察”不会出现新增联系人的记录，这是避免写入共享库的设计。

独立数据位于 SDK 分配的数据目录下的 `isolated-v2/`，常规项目布局为 `data/plugins/local.per-user-persona/isolated-v2/`。文件名由机器人与联系人 ID 的哈希生成，**不是加密存储**。备份时把这些文件当作私人聊天记录保护。

## 禁用、更新与回滚

停用本插件即可停止新增人格的独立收发。原适配器仍拒绝新增联系人，因此不会回退成麦麦人格。原联系人继续按原流程工作。

更新前备份本插件源码、四份私人配置及独立数据；确认新版本兼容后只重载本插件。回滚时恢复旧源码和配置、保留当前独立数据，再启用。不要把数据库恢复成较旧快照而丢失新聊天，也不要为回滚删除旧记录。共享记忆和核心数据库无需修改。

## 开发与验证

离线测试无需真实 QQ、API key 或模型调用：

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python tools/check_public.py
.venv/bin/python -m unittest discover -v
```

Windows 使用 `.venv\Scripts\python`。测试只在临时目录操作合成数据库，网络测试绑定本机回环地址。表情选择辅助文件的 GPL 测试夹具随源码提供，不需要安装或调用原插件的 Tool。

覆盖用户并发、独立历史和检索、伪造身份、白名单、去重、禁用、配图状态、JSON 格式与截断、生成重试上限、私有后台投影及配置工具。CI 在 Linux Python 3.12 / 3.13 运行。

与真实 MaiBot 源码相关的契约测试默认跳过；在符合条件的既有 `/MaiMBot` 环境中，可显式设置 `PERSONA_INSTALLED_HOST_TESTS=1` 运行。它们会只读检查该路径的真实源码与策略，不能把普通 CI 通过当成任意版本已经验证。

原部署的 v0.2.3 曾通过 101 项完整测试和 6 次真实模型选图验证。公开包另加默认禁用、空白示例与配置准备工具，并重新执行离线测试。私人运行报告、联系人资料、聊天记录和素材图片均不随仓库发布。

## 许可证与致谢

本项目使用 [GPL-3.0-or-later](LICENSE)。表情桥接依赖及测试夹具的来源见 [NOTICE](NOTICE)。感谢 MaiBot、MaiBot-SnowLuma-Adapter 和 Maibot_Emoji_Select_With_Text 的作者。

欢迎提交脱敏、可复现的问题和兼容性改进；维护说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。

