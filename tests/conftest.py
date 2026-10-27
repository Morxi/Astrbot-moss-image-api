"""用假的 astrbot 模块替换真实依赖，使插件可以脱离 AstrBot 测试。"""
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _install_fake_astrbot():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    comps = types.ModuleType("astrbot.api.message_components")

    api.AstrBotConfig = dict
    api.logger = logging.getLogger("astrbot")

    @dataclass
    class Plain:
        text: str

    @dataclass
    class Image:
        file: str

        @classmethod
        def fromBytes(cls, data: bytes):
            return cls(file=f"bytes:{len(data)}")

        @classmethod
        def fromURL(cls, url: str):
            return cls(file=url)

    comps.Plain, comps.Image = Plain, Image

    @dataclass
    class MessageChain:
        chain: list = field(default_factory=list)

    class _Filter:
        @staticmethod
        def command(*args, **kwargs):
            return lambda f: f

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda f: f

    event.AstrMessageEvent = object
    event.MessageChain = MessageChain
    event.filter = _Filter()

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context = object
    star.Star = Star
    star.register = lambda *args, **kwargs: (lambda cls: cls)

    api.event, api.star, api.message_components = event, star, comps
    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.message_components": comps,
        }
    )


_install_fake_astrbot()
