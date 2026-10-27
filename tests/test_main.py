import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import main
from main import MossImageError, MossImagePlugin, parse_command, parse_size


class FakeEvent:
    def __init__(self, text="", sender="u1"):
        self.message_str = text
        self.sender = sender
        self.sent = []

    def get_sender_id(self):
        return self.sender

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    async def send(self, chain):
        self.sent.append(chain)


def make_plugin(**config):
    config.setdefault("api_key", "test-key")
    return MossImagePlugin(context=None, config=config)


# ---------- parse_size ----------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("1024x1024", "1024x1024"),
        ("16:9", "1344x768"),
        ("21:9", "1536x656"),
        ("768×1344", "768x1344"),
        ("1024*512", "1024x512"),
        ("256x256", "256x256"),
        ("2048x1024", "2048x1024"),
    ],
)
def test_parse_size_valid(value, expected):
    assert parse_size(value) == expected


@pytest.mark.parametrize(
    "value", ["2048x2048", "1000x1000", "240x256", "2064x256", "abc", "1024"]
)
def test_parse_size_invalid(value):
    with pytest.raises(ValueError):
        parse_size(value)


def test_aspect_sizes_within_limits():
    for size in main.ASPECT_SIZES.values():
        assert parse_size(size) == size


# ---------- parse_command ----------

def test_parse_command_options():
    prompt, opts = parse_command("一只橘猫 --size 16:9 --seed 42")
    assert prompt == "一只橘猫"
    assert opts == {"size": "16:9", "seed": "42"}


def test_parse_command_quoted_and_flags():
    prompt, opts = parse_command('--enhance 雨后花园 --neg "low quality, blurry" --no-wm')
    assert prompt == "雨后花园"
    assert opts == {"enhance": True, "neg": "low quality, blurry", "no-wm": True}


def test_parse_command_equals_syntax():
    assert parse_command("猫 --size=768x1344") == ("猫", {"size": "768x1344"})


def test_parse_command_missing_value():
    with pytest.raises(ValueError):
        parse_command("cat --seed")


# ---------- _build_payload ----------

def test_payload_defaults():
    p = make_plugin()._build_payload("cat", {})
    assert p == {
        "model": "moss-image-base-1.0",
        "prompt": "cat",
        "size": "1024x1024",
        "n": 1,
        "response_format": "url",
        "prompt_enhance": False,
        "output_watermark": {"enabled": True},
    }


def test_payload_config_and_overrides():
    plugin = make_plugin(
        model="moss-image-sft-1.0",
        negative_prompt="nsfw",
        num_inference_steps=20,
        guidance_scale=4.5,
        prompt_enhance=True,
        watermark=False,
    )
    p = plugin._build_payload("cat", {})
    assert p["model"] == "moss-image-sft-1.0"
    assert p["negative_prompt"] == "nsfw"
    assert p["num_inference_steps"] == 20
    assert p["guidance_scale"] == 4.5
    assert p["prompt_enhance"] is True
    assert p["output_watermark"] == {"enabled": False}

    p = plugin._build_payload(
        "cat", {"no-enhance": True, "wm": True, "steps": "50", "cfg": "0", "seed": "7"}
    )
    assert p["prompt_enhance"] is False
    assert p["output_watermark"] == {"enabled": True}
    assert p["num_inference_steps"] == 50
    assert p["guidance_scale"] == 0.0  # 显式 0 需要保留
    assert p["seed"] == 7


def test_payload_unset_steps_and_guidance_omitted():
    p = make_plugin(num_inference_steps=0, guidance_scale=-1)._build_payload("cat", {})
    assert "num_inference_steps" not in p
    assert "guidance_scale" not in p


@pytest.mark.parametrize(
    "prompt,opts",
    [
        ("", {}),
        ("x" * 4001, {}),
        ("cat", {"steps": "0"}),
        ("cat", {"steps": "101"}),
        ("cat", {"cfg": "10.5"}),
        ("cat", {"seed": "-1"}),
        ("cat", {"seed": str(2**53)}),
        ("cat", {"seed": "abc"}),
    ],
)
def test_payload_invalid(prompt, opts):
    with pytest.raises(ValueError):
        make_plugin()._build_payload(prompt, opts)


# ---------- 与假 API 服务器的交互 ----------

async def image_ok(request):
    return web.Response(body=b"PNGDATA")


async def image_broken(request):
    return web.Response(status=500)


def run_with_server(handler, coro_factory, **config):
    """启动假 API 服务器，plugin 的 base_url 指向它，然后执行 coro_factory(plugin, server)。"""

    async def runner():
        app = web.Application()
        app.router.add_post("/v1/images/generations", handler)
        app.router.add_get("/img.png", image_ok)
        app.router.add_get("/broken.png", image_broken)
        server = TestServer(app)
        await server.start_server()
        plugin = make_plugin(base_url=str(server.make_url("")), **config)
        try:
            return await coro_factory(plugin, server)
        finally:
            await plugin.terminate()
            await server.close()

    return asyncio.run(runner())


def success_handler(image_path="/img.png"):
    received = {}

    async def handler(request):
        received["headers"] = dict(request.headers)
        received["json"] = await request.json()
        return web.json_response(
            {
                "created": 1,
                "data": [{"url": str(request.url.with_path(image_path))}],
                "usage": {"generated_images": 1, "output_width": 1024, "output_height": 1024},
            }
        )

    return handler, received


def test_generate_success_sends_auth_and_payload():
    handler, received = success_handler()

    async def go(plugin, server):
        return await plugin._generate({"model": "m", "prompt": "cat"})

    data = run_with_server(handler, go)
    assert data["data"][0]["url"].endswith("/img.png")
    assert received["headers"]["Authorization"] == "Bearer test-key"
    assert received["json"] == {"model": "m", "prompt": "cat"}


@pytest.mark.parametrize(
    "status,code,hint",
    [
        (400, "invalid_field_value", "请求参数不合法"),
        (403, "safety_guardrail_blocked", "安全审核"),
        (429, "rate_limit_exceeded", "请求过于频繁"),
        (502, "upstream_invalid_result", "上游"),
    ],
)
def test_generate_api_errors(status, code, hint):
    async def handler(request):
        return web.json_response({"error": {"code": code, "message": "detail"}}, status=status)

    async def go(plugin, server):
        with pytest.raises(MossImageError) as exc:
            await plugin._generate({})
        return exc.value

    err = run_with_server(handler, go)
    assert err.status == status and err.code == code
    assert hint in err.user_message() and "detail" in err.user_message()


def test_generate_non_json_error():
    async def handler(request):
        return web.Response(status=503, text="<html>bad gateway</html>")

    async def go(plugin, server):
        with pytest.raises(MossImageError) as exc:
            await plugin._generate({})
        return exc.value

    assert run_with_server(handler, go).user_message() == "HTTP 503"


def test_generate_requires_api_key():
    plugin = make_plugin(api_key="")
    with pytest.raises(MossImageError):
        asyncio.run(plugin._generate({}))


def test_draw_downloads_image():
    handler, received = success_handler()

    async def go(plugin, server):
        return await plugin._draw(FakeEvent(), "cat", {"size": "16:9"})

    chain = run_with_server(handler, go)
    assert received["json"]["size"] == "1344x768"
    assert chain == [main.Comp.Image(file="bytes:7")]


def test_draw_falls_back_to_url_when_download_fails(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(main.asyncio, "sleep", lambda s: real_sleep(0))
    handler, _ = success_handler("/broken.png")

    async def go(plugin, server):
        return await plugin._draw(FakeEvent(), "cat", {})

    chain = run_with_server(handler, go)
    assert isinstance(chain[0], main.Comp.Image)
    assert chain[0].file.endswith("/broken.png")


def test_draw_timeout_message():
    async def handler(request):
        await asyncio.sleep(5)
        return web.json_response({})

    async def go(plugin, server):
        plugin.config["timeout"] = 1
        return await plugin._draw(FakeEvent(), "cat", {})

    chain = run_with_server(handler, go)
    assert "超时" in chain[0].text


def test_draw_rejects_concurrent_task_for_same_user():
    plugin = make_plugin()
    plugin.running_users.add("u1")
    chain = asyncio.run(plugin._draw(FakeEvent(sender="u1"), "cat", {}))
    assert "进行中" in chain[0].text


def test_draw_releases_user_after_error():
    plugin = make_plugin()
    chain = asyncio.run(plugin._draw(FakeEvent(), "", {}))
    assert "参数错误" in chain[0].text
    assert "u1" not in plugin.running_users


# ---------- 指令与 LLM 工具 ----------

async def collect(agen):
    return [item async for item in agen]


@pytest.mark.parametrize("text", ["draw", "draw help", "画图 帮助"])
def test_command_help(text):
    results = asyncio.run(collect(make_plugin().draw(FakeEvent(text))))
    assert results == [("plain", main.HELP_TEXT)]


def test_command_end_to_end():
    handler, received = success_handler()

    async def go(plugin, server):
        return await collect(plugin.draw(FakeEvent("draw 一只橘猫 --seed 42 --no-wm")))

    results = run_with_server(handler, go)
    assert results[0][0] == "plain"  # 进度提示
    assert results[1] == ("chain", [main.Comp.Image(file="bytes:7")])
    assert received["json"]["prompt"] == "一只橘猫"
    assert received["json"]["seed"] == 42
    assert received["json"]["output_watermark"] == {"enabled": False}


def test_llm_tool_sends_image():
    handler, received = success_handler()
    event = FakeEvent()

    async def go(plugin, server):
        return await plugin.moss_draw_image(event, "a cat", size="9:16")

    reply = run_with_server(handler, go)
    assert "已生成" in reply
    assert received["json"]["size"] == "768x1344"
    assert event.sent[0].chain == [main.Comp.Image(file="bytes:7")]


def test_llm_tool_disabled():
    plugin = make_plugin(enable_llm_tool=False)
    assert "禁用" in asyncio.run(plugin.moss_draw_image(FakeEvent(), "cat"))
