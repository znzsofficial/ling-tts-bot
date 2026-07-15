# ling-tts-bot

基于 Xiaomi MiMo v2.5 VoiceClone 的 MaiBot 语音回复插件。

## 功能

- 默认自动选择目录中静音少、长度合适的一段，裁掉首尾静音后生成无损 WAV，减少多段语气混杂和重复有损压缩对音色的影响。
- 可切换 `balanced`（每个文件取短片段）或 `full_merge`（完整合并，兼容旧行为）参考策略。
- 支持 `llm_trigger`、`random`、`voice_only` 三种触发模式，默认 `llm_trigger` 语音优先模式。
- 语音优先模式下，普通回复默认转换为语音；LLM 判断代码、网址、表格等内容更适合文字时，可让当前回复会话内的多段消息保留文字和引用关系。
- 配置热重载后立即切换模式、API 参数和参考音频。
- 在出站阶段把纯文本原位替换为语音，避免同一回复同时发送文字和语音。
- QQ 不支持“引用消息 + 语音”组合；转换语音时会自动移除引用段，确保语音实际送达。

## 环境要求

- MaiBot 1.x
- MaiBot Plugin SDK 2.x
- Python 3.10 或更高版本
- FFmpeg（需要同时提供 `ffmpeg` 和 `ffprobe`）
- Xiaomi MiMo API Key

## 安装

在 MaiBot 根目录执行：

```bash
git clone https://github.com/Ling-LA/ling-tts-bot.git plugins/ling_tts-bot
```

也可以下载仓库源码并解压到 `MaiBot/plugins/ling_tts-bot`。首次加载后，MaiBot 会根据配置模型生成 `config.toml`；也可复制 `config.example.toml` 作为配置起点。不要把包含 API Key 的 `config.toml` 提交到 Git。

## 配置

1. 确保系统已安装 FFmpeg，或在 `voice.ffmpeg_path` 中填写可执行文件路径。
2. 在 `mimo.api_key` 中填写 MiMo API Key。
3. 在 `voice.voice_dir` 中填写参考音频目录。
4. 按需设置 `trigger.mode`，保存配置即可热重载。

触发模式：

- `llm_trigger`：默认语音优先；LLM 判断代码、网址、表格等内容更适合文字时，当前回复会话改用文字。
- `random`：按 `trigger.probability` 概率把回复转换为语音。
- `voice_only`：所有纯文本回复都转换为语音。

参考音频策略：

- `best_single`：自动挑选静音少、长度合适的一段并生成无损 WAV，默认且推荐。
- `balanced`：从目录内每个文件截取短片段并合并。
- `full_merge`：完整合并全部音频，兼容旧行为。

参考音频较多时建议保持 `voice.reference_strategy = "best_single"`。如需手动固定最接近目标音色的一段，可在 `voice.preferred_reference_file` 填写文件名；若必须让目录内每个文件都参与，则使用 `balanced`。

普通 `sk-` Key 应搭配 `https://api.xiaomimimo.com/v1`；Token Plan 地址使用 `tp-` Key，二者不能混用。

已有 `config.toml` 不必重建；未配置参考策略时默认使用 `best_single`，未配置 `trigger.mode` 时默认使用 `llm_trigger`。

## 使用示例

可使用 `/tts 你好呀` 手动验证合成与发送链路。

## 许可证

[MIT](LICENSE)
