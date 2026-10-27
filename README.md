# astrbot_plugin_moss_image

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件：调用 MOSS-Image API（`POST /v1/images/generations`）进行文生图。

## 配置

在 AstrBot 管理面板的插件配置中填写 **API Key**，其余项可按需调整：默认模型、尺寸、反向提示词、提示词增强、水印、推理步数、引导系数、超时等。

## 使用

```
/draw <提示词> [参数]
```

别名：`/画图`、`/绘图`。发送 `/draw help` 查看帮助。

| 参数 | 说明 |
| --- | --- |
| `--size 1344x768` / `--size 16:9` | 尺寸或比例（1:1 16:9 9:16 4:3 3:4 3:2 2:3 21:9） |
| `--seed 42` | 随机种子，便于复现 |
| `--steps 30` | 推理步数 1-100 |
| `--cfg 5` | 引导系数 0-10 |
| `--neg "low quality, blurry"` | 反向提示词（含空格时加引号） |
| `--model moss-image-sft-1.0` | 指定模型 |
| `--enhance` / `--no-enhance` | 开/关提示词增强 |
| `--wm` / `--no-wm` | 开/关可见水印 |

示例：

```
/draw 一只坐在窗边的橘猫，窗外是雨后的花园，柔和自然光 --size 16:9 --seed 42
```

## LLM 函数调用

插件注册了 `moss_draw_image` 工具，开启函数调用后，用户直接对 Bot 说“帮我画一张……”即可由 LLM 自动调用。可在配置中关闭。

## 说明

- 同一用户同时只能有一个进行中的绘图任务。
- 请求超时或断线时任务可能仍在执行，插件不会自动重试，以免重复计费。
- 图片会先下载再发送（失败重试 3 次），仍失败则直接发送图片 URL。
