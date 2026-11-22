"""Configuration declared through MaiBot SDK's native WebUI schema."""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="启用提醒；修改后立即停止排队任务的新发送")
    config_version: str = Field(default="1.4.0", description="配置版本")


class AlertSection(PluginConfigBase):
    __ui_label__ = "二次元图片提醒"
    __ui_icon__ = "image"
    __ui_order__ = 1

    groups: list[str] = Field(
        default_factory=list,
        description="仅这些 QQ 群生效；清空则插件待命，不会自动扩展到其他群",
    )
    target_user_id: str = Field(
        default="",
        pattern=r"^$|^[1-9]\d{4,19}$",
        description="接收原生 @ 提醒的 QQ 号；留空则插件待命不发送提醒",
    )
    cooldown_seconds: int = Field(default=0, ge=0, le=86400, description="每群提醒最短间隔，0 表示无冷却，单位秒")
    hourly_limit: int = Field(default=0, ge=0, le=100, description="每群滚动一小时最多提醒次数，0 表示不限制")
    skip_when_replied_without_source: bool = Field(
        default=True,
        description="主脑已经回复过该图片且未查到新出处时，静默跳过独立 @ 提醒，避免机械复读",
    )
    reply_grace_seconds: int = Field(
        default=6,
        ge=0,
        le=30,
        description="发出独立提醒前等待主脑首轮回复的最短观察窗口（秒），避免与主脑回复撞车",
    )


class SourceSection(PluginConfigBase):
    __ui_label__ = "以图搜图出处"
    __ui_icon__ = "search"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="命中美图后用 SauceNAO / Google Lens / IQDB（截图兜底 trace.moe）查找作品、角色、画师与原帖链接")
    enable_planner_tool: bool = Field(
        default=True,
        description="向主脑暴露 lookup_image_source 以图搜图工具，群友问出处时直接搜图回答",
    )
    include_link: bool = Field(default=True, description="命中出处后在提醒正文下一行直接附带原帖链接（Pixiv / X 优先，其次 Danbooru）")
    localize_names: bool = Field(default=True, description="用模型把角色英文名翻成通行中文译名；不确定时保留原名")
    saucenao_api_key: str = Field(default="", description="可选。填写后走 SauceNAO JSON 接口，额度更高；留空使用公开网页（自动限速）")
    serpapi_api_key: str = Field(
        default="",
        description="可选。SerpApi API Key，需同时开启 allow_temp_image_upload 才会启用 Google Lens / Bing 反搜",
    )
    allow_temp_image_upload: bool = Field(
        default=False,
        description="隐私开关：默认关闭。仅当同时填写 serpapi_api_key 且开启本项时，才允许将命中的群聊原图上传至短期匿名图床供 Google Lens / Bing 反搜",
    )
    proxy_url: str = Field(
        default="",
        description="可选。仅用于搜图请求的 HTTP 代理地址（如 http://127.0.0.1:7890），不影响 QQ 与模型连接",
    )
    min_similarity: float = Field(default=80.0, ge=50.0, le=99.0, description="SauceNAO 最低相似度（%），IQDB 固定不低于 85%")
    timeout_seconds: int = Field(default=25, ge=5, le=60, description="单个识图引擎超时，单位秒")


class AnimeAlertConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    alert: AlertSection = Field(default_factory=AlertSection)
    source: SourceSection = Field(default_factory=SourceSection)

    # Image deduplication is deliberately fixed at 24 hours in the state service.
    
__all__ = ["AnimeAlertConfig"]
