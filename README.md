# MaiBot 二次元/COS 图片提醒插件（maibot-anime-image-alert）

适用于 MaiBot SDK v2 的群聊精品二次元插画 / 真人 COS 摄影发现与提醒插件。插件通过 `chat.receive.after_process` 旁路复用宿主已完成的图片描述进行严格二次筛选，命中后按需以图搜图查明作品、角色、画师与原帖链接，并沿用机器人当前人设语气，以原生引用和 `@` 提醒指定 QQ 用户。

## 功能特性

- **零额外视觉模型开销**：不重复提交图片给 VLM，直接复用 MaiBot 宿主 `Images` 表中已完成识别的描述文本，通过纯文本快速模型（`fast_model`）进行严格结构化筛选。
- **宁缺毋滥的精品过滤**：仅命中高完成度的二次元女性角色插画、精品黑白漫画/线稿或明确真人 COS 摄影；自动排除表情包、GIF 动图、聊天/网页/游戏界面截图、普通自拍以及任何露骨或不当内容。
- **多引擎以图搜图与出处核验**：
  - 默认支持 SauceNAO、IQDB 与 Danbooru 标签补全（动画截图场景按需触发 `trace.moe`），命中结果本地 SQLite 持久化缓存（命中缓存 7 天、未命中缓存 24 小时）。
  - 可选支持 SerpApi（Google Lens / Bing 反向搜图），默认关闭第三方临时图床上传，需用户显式开启（见下方隐私说明）。
  - 可选自动将角色英文/罗马音标签翻译为通行中文译名，并在提醒文案中自然点出已核实的作品与角色信息，次行附带原帖链接。
- **防刷屏与重启持久化**：内置跨群 24 小时图片去重、每群可选冷却时间与每小时提醒上限，全部持久化在本地 SQLite 中；支持 WebUI 热更新并即时取消排队中的旧任务。
- **默认待命零打扰**：首次安装时 `alert.groups` 与 `alert.target_user_id` 默认为空，未配置前不会对任何群执行识别或发送提醒。

## 运行环境

- **MaiBot**：`1.3.19` ~ `1.4.99`（`maibot-plugin-sdk >= 2.20.0`）
- **Python 依赖**：仅使用 Python 标准库（可选启用 `Pillow` 压缩超大待搜图片，缺失时自动回退原图）

## 安装与首次配置

1. 在 MaiBot WebUI 插件市场中搜索安装「二次元/COS图片提醒」，或将本仓库放入 MaiBot 插件目录并重启/重载插件。
2. 在 WebUI 插件配置页面中填写必填项：
   - `alert.groups`：要监听的 QQ 群白名单（例如 `["200000001"]`）
   - `alert.target_user_id`：命中后要原生 `@` 提醒的接收人 QQ 号（例如 `"100000001"`）
3. 保存配置后立即生效。

## 配置项说明

| 分组 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `plugin` | `enabled` | `true` | 是否启用插件；关闭后立即停止排队与发送 |
| `alert` | `groups` | `[]` | 白名单 QQ 群列表；留空不生效 |
| `alert` | `target_user_id` | `""` | 接收原生 `@` 提醒的 QQ 号；留空不生效 |
| `alert` | `cooldown_seconds` | `0` | 单群两次提醒最短间隔（秒），0 表示不限 |
| `alert` | `hourly_limit` | `0` | 单群滚动一小时内最多提醒次数，0 表示不限 |
| `source` | `enabled` | `true` | 是否启用以图搜图查找作品、角色与原帖出处 |
| `source` | `include_link` | `true` | 命中出处后是否在正文下一行附带原帖链接 |
| `source` | `localize_names` | `true` | 是否将角色英文标签翻译为通行中文译名 |
| `source` | `saucenao_api_key` | `""` | 可选，填写后使用 SauceNAO JSON API（额度更高） |
| `source` | `serpapi_api_key` | `""` | 可选，SerpApi API Key（需同时开启 `allow_temp_image_upload` 才会生效） |
| `source` | `allow_temp_image_upload` | `false` | **隐私开关**：默认关闭；设为 `true` 且已填 `serpapi_api_key` 时，允许将命中的群聊图片上传至第三方短期匿名图床供 Google Lens / Bing 反搜 |
| `source` | `proxy_url` | `""` | 可选，仅用于搜图请求的 HTTP 代理地址（如 `http://127.0.0.1:7890`），不影响 QQ 与模型连接 |
| `source` | `min_similarity` | `80.0` | SauceNAO 最低相似度阈值（%），IQDB 固定不低于 85% |
| `source` | `timeout_seconds` | `25` | 单个搜图引擎的请求超时时间（秒） |

## 隐私与外传说明

- 启用 `source.enabled` 时，仅对**已通过白名单与精品筛选的图片**执行以图搜图，未命中图片不会对外上传。
- 默认模式下（`allow_temp_image_upload = false`），仅向 SauceNAO / IQDB / trace.moe 提交图片或向 Danbooru 查询公开元数据，**不会**向匿名公共图床上传原图。
- 如用户同时填写了 `serpapi_api_key` 并将 `allow_temp_image_upload` 显式设为 `true`，插件在 SauceNAO 未命中时会将该图片上传至支持短时自动过期的第三方匿名图床（`uguu.se` / `qu.ax` / `litterbox.catbox.moe`，1～3 小时过期），再把临时 URL 传给 SerpApi 调用 Google Lens / Bing 反搜。请在了解该行为并符合群聊隐私预期后再手动开启。

## 能力权限说明

本插件仅声明并调用以下 4 项宿主能力：

- `database.get`：只读查询宿主 `Images` 表中已完成 VLM 识别的图片描述与本地路径
- `config.get`：只读获取机器人当前人设配置，用于生成贴合人设的简短提醒话术
- `llm.generate`：调用 `fast_model` 进行结构化筛选与角色译名，调用 `replyer` 生成简短提醒正文
- `send.hybrid`：在白名单原群内以原生引用 + 原生 `@` 发送单行提醒文本

## 运行单元测试

```bash
python -m unittest discover -p "test_*.py"
```

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
