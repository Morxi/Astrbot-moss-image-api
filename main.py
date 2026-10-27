import asyncio
import re

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

DEFAULT_BASE_URL = "https://api.mosi.cn"
MODELS = (
    "moss-image-base-1.0",
    "moss-image-base-1.0-2026-10-09",
    "moss-image-sft-1.0",
    "moss-image-sft-1.0-2026-10-09",
)
MAX_PROMPT_LEN = 4000
MAX_PIXELS = 2097152
MAX_SEED = 2**53 - 1

# 常用比例 -> 尺寸（均为 16 的倍数，且总像素不超过 MAX_PIXELS）
ASPECT_SIZES = {
    "1:1": "1024x1024",
    "4:3": "1152x864",
    "3:4": "864x1152",
    "16:9": "1344x768",
    "9:16": "768x1344",
    "3:2": "1216x816",
    "2:3": "816x1216",
    "21:9": "1536x656",
}

# 友好的错误提示，key 为 API 返回的 error.code
ERROR_HINTS = {
    "invalid_field_value": "请求参数不合法",
    "forbidden": "API Key 无效或无权限",
    "prompt_enhancement_refused": "提示词增强模型拒绝了该请求",
    "safety_guardrail_blocked": "请求被平台安全审核拦截",
    "resource_not_found": "生成的图片资源不存在，请重新生成",
    "concurrency_limit_exceeded": "进行中的任务过多，请等当前任务完成后再试",
    "rate_limit_exceeded": "请求过于频繁，请稍后再试",
    "prompt_enhancement_incomplete": "提示词增强结果被截断",
    "upstream_invalid_result": "上游返回结果校验失败",
}

# 匹配 --key value / --flag 形式的参数
OPTION_RE = re.compile(r"(?:^|\s)--([a-zA-Z][a-zA-Z_-]*)(?:[=\s]+(\"[^\"]*\"|'[^']*'|[^\s-][^\s]*))?")
FLAG_OPTIONS = {"enhance", "no-enhance", "wm", "no-wm"}

HELP_TEXT = """MOSS 绘图用法：
/draw <提示词> [参数]
参数：
  --size 宽x高 或 比例(1:1 16:9 9:16 4:3 3:4 3:2 2:3 21:9)
  --seed 随机种子
  --steps 推理步数(1-100)
  --cfg 引导系数(0-10)
  --neg "反向提示词"
  --model 模型ID
  --enhance / --no-enhance 开/关提示词增强
  --wm / --no-wm 开/关可见水印
示例：/draw 一只坐在窗边的橘猫，雨后花园 --size 16:9 --seed 42"""


class MossImageError(Exception):
    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status

    def user_message(self) -> str:
        hint = ERROR_HINTS.get(self.code)
        detail = str(self)
        if hint and detail and detail != hint:
            return f"{hint}：{detail}"
        return hint or detail or f"HTTP {self.status}"


def parse_size(value: str) -> str:
    value = value.strip().lower().replace("×", "x").replace("*", "x")
    if value in ASPECT_SIZES:
        return ASPECT_SIZES[value]
    m = re.fullmatch(r"(\d+)x(\d+)", value)
    if not m:
        raise ValueError(f"尺寸格式错误：{value}，应为 宽x高 或比例如 16:9")
    w, h = int(m.group(1)), int(m.group(2))
    for side in (w, h):
        if not 256 <= side <= 2048 or side % 16:
            raise ValueError("宽高需在 256-2048 之间且为 16 的倍数")
    if w * h > MAX_PIXELS:
        raise ValueError(f"总像素 {w * h} 超过上限 {MAX_PIXELS}")
    return f"{w}x{h}"


def parse_command(text: str) -> tuple[str, dict]:
    """从指令文本中拆出提示词和 --参数。"""
    options = {}

    def _take(m: re.Match) -> str:
        key = m.group(1).lower()
        val = m.group(2)
        if key in FLAG_OPTIONS:
            options[key] = True
            # flag 后面跟的其实是提示词的一部分，放回去
            return f" {val}" if val else " "
        if val is None:
            raise ValueError(f"参数 --{key} 缺少值")
        options[key] = val.strip("\"'")
        return " "

    prompt = OPTION_RE.sub(_take, text).strip()
    return prompt, options


@register(
    "astrbot_plugin_moss_image",
    "Morxi",
    "调用 MOSS-Image API 进行文生图",
    "1.0.0",
    "https://github.com/Morxi/Astrbot-moss-image-api",
)
class MossImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        # 同一用户同时只允许一个任务，避免触发并发限制
        self.running_users: set[str] = set()

    async def initialize(self):
        self.session = aiohttp.ClientSession()

    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def _build_payload(self, prompt: str, options: dict) -> dict:
        cfg = self.config
        if not prompt:
            raise ValueError("提示词不能为空")
        if len(prompt) > MAX_PROMPT_LEN:
            raise ValueError(f"提示词过长（{len(prompt)}/{MAX_PROMPT_LEN}）")

        model = options.get("model") or cfg.get("model", MODELS[0])
        payload = {
            "model": model,
            "prompt": prompt,
            "size": parse_size(options.get("size") or cfg.get("size", "1024x1024")),
            "n": 1,
            "response_format": "url",
        }

        if "enhance" in options:
            payload["prompt_enhance"] = True
        elif "no-enhance" in options:
            payload["prompt_enhance"] = False
        else:
            payload["prompt_enhance"] = bool(cfg.get("prompt_enhance", False))

        if "wm" in options:
            watermark = True
        elif "no-wm" in options:
            watermark = False
        else:
            watermark = bool(cfg.get("watermark", True))
        payload["output_watermark"] = {"enabled": watermark}

        negative = options.get("neg", cfg.get("negative_prompt", ""))
        if negative:
            if len(negative) > MAX_PROMPT_LEN:
                raise ValueError("反向提示词过长")
            payload["negative_prompt"] = negative

        if "seed" in options:
            seed = int(options["seed"])
            if not 0 <= seed <= MAX_SEED:
                raise ValueError("seed 超出范围")
            payload["seed"] = seed

        steps = options.get("steps", cfg.get("num_inference_steps", 0))
        if steps:
            steps = int(steps)
            if not 1 <= steps <= 100:
                raise ValueError("推理步数需在 1-100 之间")
            payload["num_inference_steps"] = steps

        guidance = options.get("cfg")
        if guidance is None and cfg.get("guidance_scale", -1) >= 0:
            guidance = cfg.get("guidance_scale")
        if guidance is not None:
            guidance = float(guidance)
            if not 0 <= guidance <= 10:
                raise ValueError("引导系数需在 0-10 之间")
            payload["guidance_scale"] = guidance

        return payload

    async def _generate(self, payload: dict) -> dict:
        api_key = self.config.get("api_key", "")
        if not api_key:
            raise MossImageError("未配置 API Key，请在插件配置中填写")
        base_url = (self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        timeout = aiohttp.ClientTimeout(total=int(self.config.get("timeout", 900)))
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 注意：超时/断线时任务可能仍在执行，按官方建议不自动重试
        async with self._get_session().post(
            f"{base_url}/v1/images/generations",
            json=payload,
            headers=headers,
            timeout=timeout,
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            if resp.status != 200:
                err = (data or {}).get("error") or {}
                if isinstance(err, str):
                    err = {"message": err}
                raise MossImageError(
                    err.get("message", "") or f"HTTP {resp.status}",
                    code=err.get("code", ""),
                    status=resp.status,
                )
            if not data or not data.get("data"):
                raise MossImageError("API 返回中没有图片")
            return data

    async def _download(self, url: str) -> bytes | None:
        """下载图片，失败则重试；最终失败返回 None，由调用方回退为直接发 URL。"""
        for attempt in range(3):
            try:
                async with self._get_session().get(
                    url, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except Exception as e:
                logger.warning(f"[moss_image] 下载图片失败（第 {attempt + 1} 次）：{e}")
                await asyncio.sleep(1 + attempt)
        return None

    async def _draw(self, event: AstrMessageEvent, prompt: str, options: dict) -> list:
        """执行一次绘图，返回要发送的消息组件。异常已转换为可读文本。"""
        user_id = event.get_sender_id()
        if user_id in self.running_users:
            return [Comp.Plain("你已有一个绘图任务在进行中，请稍候")]

        self.running_users.add(user_id)
        try:
            payload = self._build_payload(prompt, options)
            logger.info(
                f"[moss_image] {user_id} 请求绘图：model={payload['model']} size={payload['size']}"
            )
            data = await self._generate(payload)
        except ValueError as e:
            return [Comp.Plain(f"参数错误：{e}")]
        except MossImageError as e:
            logger.error(f"[moss_image] API 错误 {e.status} {e.code}：{e}")
            return [Comp.Plain(f"绘图失败：{e.user_message()}")]
        except asyncio.TimeoutError:
            return [Comp.Plain("绘图请求超时，任务可能仍在执行，请稍后再试（不要立即重复提交）")]
        except aiohttp.ClientError as e:
            logger.error(f"[moss_image] 网络错误：{e}")
            return [Comp.Plain(f"绘图失败：网络错误 {e}")]
        finally:
            self.running_users.discard(user_id)

        url = data["data"][0]["url"]
        image_bytes = await self._download(url)
        image = Comp.Image.fromBytes(image_bytes) if image_bytes else Comp.Image.fromURL(url)
        chain = [image]
        if self.config.get("show_info", False):
            usage = data.get("usage") or {}
            info = f"{payload['model']} {usage.get('output_width')}x{usage.get('output_height')}"
            if "seed" in payload:
                info += f" seed={payload['seed']}"
            chain.append(Comp.Plain(info))
        return chain

    @filter.command("draw", alias={"画图", "绘图"})
    async def draw(self, event: AstrMessageEvent):
        """使用 MOSS-Image 文生图。/draw help 查看用法"""
        text = re.sub(r"^\s*/?\S+", "", event.message_str, count=1).strip()
        if not text or text in ("help", "帮助"):
            yield event.plain_result(HELP_TEXT)
            return

        try:
            prompt, options = parse_command(text)
        except ValueError as e:
            yield event.plain_result(f"参数错误：{e}")
            return

        if self.config.get("send_progress", True):
            yield event.plain_result("正在绘制，请稍候…")
        chain = await self._draw(event, prompt, options)
        yield event.chain_result(chain)

    @filter.llm_tool(name="moss_draw_image")
    async def moss_draw_image(
        self, event: AstrMessageEvent, prompt: str, size: str = "", negative_prompt: str = ""
    ):
        """根据文字描述生成一张图片并直接发送给用户。当用户要求画图、生成图片时调用。

        Args:
            prompt(string): 详细的画面描述，可使用中文或英文，建议包含主体、场景、风格、光线等
            size(string): 可选，图片尺寸，格式为 宽x高（如 1024x1024）或比例（如 16:9、9:16、1:1）
            negative_prompt(string): 可选，不希望出现在画面中的内容
        """
        if not self.config.get("enable_llm_tool", True):
            return "绘图工具已被管理员禁用"
        options = {}
        if size:
            options["size"] = size
        if negative_prompt:
            options["neg"] = negative_prompt
        chain = await self._draw(event, prompt, options)
        if isinstance(chain[0], Comp.Plain):
            return chain[0].text
        await event.send(MessageChain(chain=chain))
        return "图片已生成并发送给用户。"
