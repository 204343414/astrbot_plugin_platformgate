"""
平台能力门禁 (astrbot_plugin_platformgate)

按平台(napcat/onebot11=aiocqhttp 与 QQ 官方=qq_official)白名单控制哪些插件
的指令和 LLM 工具可用。默认全部拦截：在 WebUI 里勾选后，才允许该插件在对应
平台执行指令和调用工具。

设计动机：
  某些插件(群分析、QQ空间等)在 OneBot11(napcat) 上运行容易导致账号封禁。
  本插件从源头把它们从易封号平台的白名单里排除，拦截其指令与 LLM 工具。

实现要点（均经 AstrBot 源码确认）：
  - 枚举插件: astrbot.core.star.star.star_registry / star_map
  - 枚举指令: astrbot.core.star.register.star_handler.star_handlers_registry
              中 event_filters 里的 CommandFilter(command_name/alias/parent)
  - 指令拦截: 高 priority 的 @filter.event_message_type(ALL) + event.stop_event()
  - LLM 工具: FunctionTool 有 handler_module_path, 可用 func_tool_manager 反查
              插件; 拦截用 @filter.on_using_llm_tool() 动态包装 handler
              使被拒工具调用返回"该平台不可用"。
  - WebUI: pages/ + ctx.register_web_api + astrbot.api.web
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_platformgate"

# 本插件只关注这两个平台，避免误伤其它平台。
PLATFORM_KEYS = ("aiocqhttp", "qq_official")


# ---------------------------------------------------------------
# 数据模型：注册表快照
# ---------------------------------------------------------------
@dataclass
class PluginEntry:
    name: str
    display_name: str = ""
    author: str = ""
    version: str = ""
    desc: str = ""
    activated: bool = True
    reserved: bool = False
    commands: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


class Snapshot:
    """一次注册表枚举快照：插件列表 + 每个插件的指令和工具。"""

    def __init__(self) -> None:
        self.plugins: list[PluginEntry] = []
        self.ts: float = time.time()

    def plugin_by_name(self, name: str) -> PluginEntry | None:
        for p in self.plugins:
            if p.name == name:
                return p
        return None


def _normalize_cmd_name(cmd: str) -> str:
    """去首斜杠、去首尾空格、压缩空白，用于匹配。"""
    s = cmd.strip()
    s = re.sub(r"\s+", " ", s)
    return s.lstrip("/")


# ---------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------
@register(
    PLUGIN_NAME,
    "平台能力门禁 Platform Gate",
    "按平台白名单控制哪些插件的指令和 LLM 工具可用，防止高危插件在易封号平台运行。",
    "1.0.0",
    "https://github.com/204343414/astrbot_plugin_platformgate",
)
class PlatformGatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.ctx = context
        self.cfg = config
        self.logger = logging.getLogger("astrbot")

        self.enabled = bool(config.get("enabled", True))
        self.default_allow_all = bool(config.get("default_allow_all", False))
        self.block_hint = bool(config.get("block_hint", False))
        self.block_hint_text = str(config.get("block_hint_text", "该功能 ({plugin}) 在此平台不可用。"))
        self.enum_interval = max(int(config.get("enumerate_interval_seconds", 15)), 0)
        self.protect_own = bool(config.get("protect_own_plugin", True))
        self.debug = bool(config.get("debug_log", False))

        # 按平台拦截 LLM 说话：在这些平台(逗号分隔)上禁止 AstrBot 调 LLM 回复。
        # 典型场景：官方 QQ bot 不能 LLM 主动说话(受平台限制/易触发风控)，而 napcat 可以。
        self.llm_block_platforms = {
            s.strip() for s in str(config.get("block_llm_speech_platforms", "qq_official")).split(",") if s.strip()
        }

        self.data_file = self._resolve_data_file()
        # rules[platform][plugin_name] = True/False （None 表示未配置 -> 用默认策略）
        self.rules: dict[str, dict[str, bool]] = {"aiocqhttp": {}, "qq_official": {}}
        # 允许 LLM 在指定群聊发言；空列表表示不额外限制（兼容原有行为）。
        self.speech_groups: dict[str, list[str]] = {"aiocqhttp": [], "qq_official": []}
        self._load_rules()

        self._snapshot: Snapshot | None = None
        self._snapshot_locked: set[str] = set()  # 已包装 handler 的 tool 名(避免重复包装)

        logger.info(
            "[PlatformGate] 加载完成 enabled=%s default_allow_all=%s platforms=%s",
            self.enabled, self.default_allow_all, list(self.rules.keys()),
        )

    # ---------------- 持久化 ----------------
    def _resolve_data_file(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            root = Path(get_astrbot_data_path())
        except (ImportError, AttributeError, TypeError):
            root = Path("data").resolve()
        directory = root / "plugin_data" / PLUGIN_NAME
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "rules.json"

    def _load_rules(self) -> None:
        try:
            if self.data_file.exists():
                raw = json.loads(self.data_file.read_text(encoding="utf-8"))
                for plat in PLATFORM_KEYS:
                    if isinstance(raw.get(plat), dict):
                        self.rules[plat] = {str(k): bool(v) for k, v in raw[plat].items()}
                    groups = raw.get("speech_groups", {}).get(plat, [])
                    if isinstance(groups, list):
                        self.speech_groups[plat] = [str(x).strip() for x in groups if str(x).strip()]
        except Exception as exc:
            self.logger.error("[PlatformGate] 规则读取失败: %s", exc)

    def _save_rules(self) -> None:
        tmp = self.data_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**self.rules, "speech_groups": self.speech_groups}, f, ensure_ascii=False, indent=2)
            f.flush()
            import os
            os.fsync(f.fileno())
        tmp.replace(self.data_file)

    # ---------------- 规则查询 ----------------
    def _is_allowed(self, platform: str, plugin_name: str) -> bool:
        """判断某插件在某平台是否放行。未显式配置 -> 默认策略。"""
        if not self.enabled:
            return True
        if self.protect_own and plugin_name == PLUGIN_NAME:
            return True
        if platform not in self.rules:
            return not self.default_allow_all  # 未知平台保守拦截
        val = self.rules[platform].get(plugin_name)
        if val is None:
            return self.default_allow_all
        return bool(val)

    # ---------------- 枚举注册表 ----------------
    def _refresh_snapshot(self, force: bool = False) -> Snapshot:
        now = time.time()
        if not force and self._snapshot is not None and (now - self._snapshot.ts) < self.enum_interval:
            return self._snapshot
        snap = Snapshot()
        self._enumerate_plugins(snap)
        self._snapshot = snap
        return snap

    def _enumerate_plugins(self, snap: Snapshot) -> None:
        try:
            from astrbot.core.star.star import star_registry, star_map
        except Exception as exc:
            self.logger.error("[PlatformGate] 无法导入 star 注册表: %s", exc)
            return
        # name -> PluginEntry
        by_name: dict[str, PluginEntry] = {}
        for meta in star_registry:
            if meta is None:
                continue
            name = meta.name or meta.root_dir_name or ""
            if not name:
                continue
            ent = by_name.get(name)
            if ent is None:
                ent = PluginEntry(
                    name=name,
                    display_name=meta.display_name or name,
                    author=meta.author or "",
                    version=meta.version or "",
                    desc=meta.desc or "",
                    activated=meta.activated,
                    reserved=meta.reserved,
                )
                by_name[name] = ent
        # 指令
        try:
            from astrbot.core.star.register.star_handler import star_handlers_registry
            from astrbot.core.star.filter.command import CommandFilter
            from astrbot.core.star.filter.command_group import CommandGroupFilter
        except Exception as exc:
            self.logger.error("[PlatformGate] 无法导入 handler 注册表: %s", exc)
            star_handlers_registry = None
        if star_handlers_registry is not None:
            for handler in star_handlers_registry:
                module_path = getattr(handler, "handler_module_path", None)
                plugin = star_map.get(module_path) if star_map else None
                plugin_name = (plugin.name if plugin and plugin.name else module_path or "")
                if not plugin_name:
                    continue
                ent = by_name.get(plugin_name) or PluginEntry(name=plugin_name, display_name=plugin_name)
                by_name[plugin_name] = ent
                for flt in handler.event_filters:
                    if isinstance(flt, CommandFilter):
                        for full in flt.get_complete_command_names():
                            n = _normalize_cmd_name(full)
                            if n and n not in ent.commands:
                                ent.commands.append(n)
                    elif isinstance(flt, CommandGroupFilter):
                        for full in flt.get_complete_command_names():
                            n = _normalize_cmd_name(full)
                            if n and n not in ent.commands:
                                ent.commands.append(n)
        # LLM 工具
        self._enumerate_tools(by_name)
        snap.plugins = [by_name[k] for k in by_name]
        snap.plugins.sort(key=lambda p: p.display_name)

    def _enumerate_tools(self, by_name: dict[str, PluginEntry]) -> None:
        try:
            mgr = self.ctx.get_llm_tool_manager()
            from astrbot.core.star.star import star_map
        except Exception:
            return
        for tool in list(mgr.func_list):
            name = getattr(tool, "name", "") or ""
            if not name:
                continue
            mp = getattr(tool, "handler_module_path", None) or ""
            plugin = star_map.get(mp) if star_map else None
            plugin_name = plugin.name if plugin and plugin.name else (mp or "")
            if not plugin_name:
                continue
            ent = by_name.get(plugin_name) or PluginEntry(name=plugin_name, display_name=plugin_name)
            by_name[plugin_name] = ent
            if name not in ent.tools:
                ent.tools.append(name)

    # ---------------- 指令拦截 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def gate_commands(self, event: AstrMessageEvent):
        if not self.enabled:
            return
        platform = event.get_platform_name()
        if platform not in PLATFORM_KEYS:
            return
        if event.is_admin():
            return
        text = (event.get_message_str() or "").strip()
        if not text:
            return
        cmd = _normalize_cmd_name(text)
        first_token = cmd.split(" ", 1)[0]
        snap = self._refresh_snapshot()
        hit_plugin: str | None = None
        for ent in snap.plugins:
            # 跳过内置(reserved)插件：AstrBot 系统命令(/reset /help /sid 等)
            # 不属于任何第三方插件，不应被门禁拦截，永远放行。
            if ent.reserved:
                continue
            if not ent.commands:
                continue
            for c in ent.commands:
                if cmd == c or cmd.startswith(c + " ") or first_token == c:
                    if self._is_allowed(platform, ent.name):
                        continue
                    hit_plugin = ent.name
                    break
            if hit_plugin:
                break
        if hit_plugin:
            # 跨平台静默让位：若该插件在"另一个目标平台"放行，则本平台静默拦截
            # (不发"不可用"提示)，让那个平台的 bot 去响应，避免两个 bot 各自
            # 提示/触发造成刷屏。前提是用户在门禁里把插件配成"官方放行/napcat不放行"
            # 之类的差异配置，即可实现"优先官方说话"。
            other_ok = any(
                other != platform
                and other in PLATFORM_KEYS
                and self._is_allowed(other, hit_plugin)
                for other in PLATFORM_KEYS
            )
            logger.info(
                "[PlatformGate] 拦截指令 platform=%s plugin=%s cmd=%r other_ok=%s",
                platform, hit_plugin, cmd[:60], other_ok,
            )
            event.stop_event()
            # 另一平台会响应 -> 完全静默让位；否则按配置决定是否提示。
            if self.block_hint and not other_ok:
                await self._send_hint(event, hit_plugin, cmd)
        elif self.debug:
            logger.info("[PlatformGate] 放行/未命中 platform=%s text=%r", platform, text[:60])

    async def _send_hint(self, event: AstrMessageEvent, plugin: str, command: str):
        text = self.block_hint_text.replace("{plugin}", plugin).replace("{command}", command)
        try:
            await event.send(self._chain(text))
        except Exception as exc:
            self.logger.warning("[PlatformGate] 拦截提示发送失败: %s", exc)

    @staticmethod
    def _chain(text: str):
        from astrbot.api.event import MessageChain
        return MessageChain([Plain(text)])

    # ---------------- LLM 工具拦截 ----------------
    @filter.on_using_llm_tool()
    async def gate_tools(self, event: AstrMessageEvent, tool, tool_args: dict | None):
        if not self.enabled:
            return
        platform = event.get_platform_name()
        if platform not in PLATFORM_KEYS:
            return
        if event.is_admin():
            return
        tool_name = getattr(tool, "name", "") or ""
        if not tool_name:
            return
        plugin_name = self._plugin_of_tool(tool_name)
        if not plugin_name:
            return
        if self._is_allowed(platform, plugin_name):
            return
        self.logger.info("[PlatformGate] 拦截工具 platform=%s plugin=%s tool=%s", platform, plugin_name, tool_name)
        if self.block_hint:
            await self._send_hint(event, plugin_name, tool_name)
        # 包装 handler，使本次调用返回"不可用"而非真正执行
        self._wrap_tool_to_block(tool, plugin_name)

    def _plugin_of_tool(self, tool_name: str) -> str | None:
        try:
            mgr = self.ctx.get_llm_tool_manager()
            from astrbot.core.star.star import star_map
        except Exception:
            return None
        tool = mgr.get_func(tool_name)
        if tool is None:
            return None
        mp = getattr(tool, "handler_module_path", None) or ""
        plugin = star_map.get(mp) if star_map else None
        return plugin.name if plugin and plugin.name else (mp or None)

    def _wrap_tool_to_block(self, tool, plugin_name: str):
        """把 tool.handler 替换为一个按平台动态判断的包装器。

        拦截时返回"该平台不可用"；放行时调用原 handler，保证不误伤其它平台。
        """
        if tool_name := getattr(tool, "name", ""):
            if tool_name in self._snapshot_locked:
                return
            self._snapshot_locked.add(tool_name)
        orig = getattr(tool, "handler", None)

        async def _blocked(event, **kwargs):
            # 动态判断：若当前平台其实放行，则调原 handler
            platform = event.get_platform_name()
            if self._is_allowed(platform, plugin_name):
                if orig is not None:
                    res = orig(event, **kwargs)
                    if inspect.isasyncgen(res):
                        last = None
                        async for item in res:
                            last = item
                        return last
                    if inspect.iscoroutine(res):
                        return await res
                    return res
                return "该工具已放行。"
            return f"该功能 ({plugin_name}) 在此平台不可用。"

        # 保留原 handler 引用，供放行时调用
        if orig is not None:
            try:
                setattr(tool, "_gate_original_handler", orig)
            except Exception:
                pass
        try:
            tool.handler = _blocked
        except Exception as exc:
            self.logger.warning("[PlatformGate] 包装工具失败 %s: %s", getattr(tool, "name", "?"), exc)

    # ---------------- 按平台拦截 LLM 说话 ----------------
    @filter.on_llm_request()
    async def gate_llm_speech(self, event: AstrMessageEvent, req):
        """按平台禁止 LLM 说话。

        AstrBot 在每次 LLM 请求前触发 on_llm_request 钩子；若此处
        event.stop_event()，则整个 LLM 请求被跳过（agent_request 里
        call_event_hook 返回 True 即 return）。
        """
        if not self.enabled:
            return
        platform = event.get_platform_name()
        if platform not in PLATFORM_KEYS:
            return
        # 填写群白名单后，无论平台是否在全局拦截列表，都只允许这些群触发 LLM。
        groups = self.speech_groups.get(platform, [])
        if groups:
            group_id = str(event.get_group_id() or "").strip()
            if group_id not in groups:
                logger.info("[PlatformGate] 拦截非白名单群的 LLM 说话 platform=%s group=%s", platform, group_id or "private")
                event.stop_event()
            return
        if platform not in self.llm_block_platforms:
            return
        # 管理员放行（可选）：管理员在拦截平台也能用 LLM
        if event.is_admin():
            return
        logger.info(
            "[PlatformGate] 拦截 LLM 说话 platform=%s (block_llm_speech_platforms=%s)",
            platform, sorted(self.llm_block_platforms),
        )
        event.stop_event()

    # ---------------- WebUI API ----------------
    def register_web_routes(self) -> None:
        from astrbot.api.web import request, json_response, error_response

        self.ctx.register_web_api(f"/{PLUGIN_NAME}/bootstrap", self._web_bootstrap, ["GET"], "PlatformGate 配置数据")
        self.ctx.register_web_api(f"/{PLUGIN_NAME}/set", self._web_set, ["POST"], "PlatformGate 保存规则")
        self.ctx.register_web_api(f"/{PLUGIN_NAME}/refresh", self._web_refresh, ["POST"], "PlatformGate 强制刷新注册表")
        self.ctx.register_web_api(f"/{PLUGIN_NAME}/speech-groups", self._web_speech_groups, ["POST"], "保存允许 LLM 发言的群")

    async def _web_bootstrap(self):
        from astrbot.api.web import json_response
        snap = self._refresh_snapshot(force=True)
        payload = {
            "enabled": self.enabled,
            "default_allow_all": self.default_allow_all,
            "block_hint": self.block_hint,
            "block_hint_text": self.block_hint_text,
            "llm_block_platforms": sorted(self.llm_block_platforms),
            "speech_groups": self.speech_groups,
            "rules": self.rules,
            "platforms": list(PLATFORM_KEYS),
            "plugins": [
                {
                    "name": p.name,
                    "display_name": p.display_name,
                    "author": p.author,
                    "version": p.version,
                    "desc": p.desc,
                    "activated": p.activated,
                    "reserved": p.reserved,
                    "commands": p.commands,
                    "tools": p.tools,
                }
                for p in snap.plugins
                if p.name != PLUGIN_NAME and not p.reserved  # 不显示门禁自身与内置插件
            ],
        }
        return json_response(payload)

    async def _web_set(self):
        from astrbot.api.web import request, json_response, error_response
        body = await request.json(default={})
        platform = str(body.get("platform", "") or "")
        plugin = str(body.get("plugin", "") or "")
        allow = body.get("allow")
        if platform not in PLATFORM_KEYS:
            return error_response("未知平台", status_code=400)
        if not plugin:
            return error_response("缺少插件名", status_code=400)
        if allow is None:
            self.rules[platform].pop(plugin, None)  # 复位为默认
        else:
            self.rules[platform][plugin] = bool(allow)
        self._save_rules()
        return json_response({"platform": platform, "plugin": plugin, "allow": allow, "effective": self._is_allowed(platform, plugin)})

    async def _web_speech_groups(self):
        from astrbot.api.web import request, json_response, error_response
        body = await request.json(default={})
        platform = str(body.get("platform", "") or "")
        if platform not in PLATFORM_KEYS:
            return error_response("未知平台", status_code=400)
        groups = body.get("groups", [])
        if not isinstance(groups, list):
            return error_response("groups 必须是数组", status_code=400)
        self.speech_groups[platform] = list(dict.fromkeys(str(x).strip() for x in groups if str(x).strip()))
        self._save_rules()
        return json_response({"platform": platform, "groups": self.speech_groups[platform]})

    async def _web_refresh(self):
        from astrbot.api.web import json_response
        snap = self._refresh_snapshot(force=True)
        return json_response({"plugins": [{"name": p.name, "display_name": p.display_name} for p in snap.plugins]})

    async def initialize(self) -> None:
        self.register_web_routes()
        # 启动诊断：确认门禁插件已被 AstrBot 加载，并枚举到多少插件/指令
        try:
            snap = self._refresh_snapshot(force=True)
            logger.info(
                "[PlatformGate] 已加载并枚举到 %d 个插件。rules.aiocqhttp=%d rules.qq_official=%d",
                len(snap.plugins), len(self.rules.get("aiocqhttp", {})), len(self.rules.get("qq_official", {})),
            )
            if self.debug:
                for p in snap.plugins:
                    logger.info(
                        "[PlatformGate]   插件 %s | 指令=%s | 工具=%s",
                        p.name, p.commands, p.tools,
                    )
        except Exception as exc:
            logger.warning("[PlatformGate] initialize 枚举诊断失败: %s", exc)
        # 把"哪些平台 LLM 被门禁拦截"写入全局 SharedPreferences，
        # 供 qqhub 等其它插件读取，让它们知道该平台的 LLM 实际不可用。
        try:
            from astrbot.core import sp
            await sp.global_put("platformgate_llm_blocked_platforms", sorted(self.llm_block_platforms))
            logger.info("[PlatformGate] 已发布 LLM 拦截平台到全局标记: %s", sorted(self.llm_block_platforms))
        except Exception as exc:
            logger.warning("[PlatformGate] 写入全局 LLM 拦截标记失败: %s", exc)

    async def terminate(self) -> None:
        self._save_rules()
