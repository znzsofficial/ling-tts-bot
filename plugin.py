# -*- coding: utf-8 -*-
"""MiMo v2.5 语音克隆插件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ActivationType, ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

import aiohttp


AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".mpeg", ".ogg", ".opus", ".wav", ".wma"})
TEXT_REPLY_TOOL_NAME = "ling_text_reply"
FFMPEG_TIMEOUT_SECONDS = 60
REFERENCE_CACHE_NAME = re.compile(r"^reference-[0-9a-f]{64}\.(?:wav|mp3)$")
PLUGIN_NOTICE_EXACT = frozenset(
    {
        "测试命令未对你开放",
        "语音已发送",
        "文字和语音已发送",
        "语音发送失败",
        "缺少真实聊天流 session_id",
        "用法：/tts <文本>",
    }
)
PRESET_MODEL = "mimo-v2.5-tts"
VOICECLONE_MODEL = "mimo-v2.5-tts-voiceclone"
PRESET_VOICES = (
    "mimo_default",
    "冰糖",
    "茉莉",
    "苏打",
    "白桦",
    "Mia",
    "Chloe",
    "Milo",
    "Dean",
)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="2.5.0", description="配置版本")


class GeneralConfig(PluginConfigBase):
    """通用请求配置。"""

    __ui_label__ = "通用"

    max_text_length: int = Field(default=300, ge=1, le=2000, description="单次合成最大文本长度")
    timeout: int = Field(default=120, ge=5, le=300, description="MiMo 请求超时秒数")


class TriggerConfig(PluginConfigBase):
    """语音触发配置。"""

    __ui_label__ = "触发方式"

    mode: Literal["llm_trigger", "random", "voice_only"] = Field(
        default="llm_trigger",
        description="llm_trigger=优先语音且LLM可改用文字，random=按概率转换，voice_only=所有纯文本转语音",
    )
    probability: float = Field(default=0.3, ge=0.0, le=1.0, description="random 模式的语音概率")
    random_decision_ttl: float = Field(
        default=8.0,
        ge=0.5,
        le=60.0,
        description="同一聊天流短时间内复用随机决定，避免一条回复中语音文字混发",
    )
    text_override_ttl: float = Field(
        default=15.0,
        ge=1.0,
        le=60.0,
        description="LLM 选择文字后，当前回复会话保持文字的有效秒数",
    )


class VoiceConfig(PluginConfigBase):
    """参考音频配置。"""

    __ui_label__ = "音色克隆"

    voice_dir: str = Field(default="", description="参考音频目录")
    clone_prompt: str = Field(default="用原本的音色和语气说话，保持自然流畅")
    ffmpeg_path: str = Field(default="", description="FFmpeg 可执行文件路径；留空时从 PATH 查找")
    reference_strategy: Literal["best_single", "balanced", "full_merge"] = Field(
        default="best_single",
        description="best_single=自动选最佳单段，balanced=每段取短片段，full_merge=完整合并",
    )
    preferred_reference_file: str = Field(
        default="",
        description="指定 best_single 使用的文件名；留空时自动选择",
    )
    max_clip_duration: float = Field(default=15.0, ge=3.0, le=30.0, description="最佳单段的最长秒数")
    balanced_clip_duration: float = Field(default=3.0, ge=1.0, le=10.0, description="均衡模式每个文件截取秒数")
    silence_threshold_db: float = Field(default=-45.0, ge=-80.0, le=-20.0, description="首尾静音判定阈值")
    sample_rate: int = Field(default=24000, ge=8000, le=48000, description="参考音频采样率")
    reference_bitrate_kbps: int = Field(default=64, ge=16, le=320, description="完整合并模式的 MP3 码率")
    max_reference_base64_mb: float = Field(
        default=9.5,
        gt=0,
        le=10,
        description="参考音频 Base64 上限；MiMo 官方上限为 10 MB",
    )


class OutputConfig(PluginConfigBase):
    """出站内容配置。"""

    __ui_label__ = "输出"

    mode: Literal["voice_only", "text_and_voice"] = Field(
        default="text_and_voice",
        description="voice_only=只发语音并替换原文；text_and_voice=保留文字，并另发一条语音",
    )


class CommandConfig(PluginConfigBase):
    """测试命令白名单。留空时 /tts 不生效。"""

    __ui_label__ = "测试命令"

    allowed_user_ids: List[str] = Field(
        default_factory=list,
        description="允许使用 /tts、/voice、/mimo 的 QQ 号。留空时命令不生效",
    )


class MiMoConfig(PluginConfigBase):
    """MiMo API 配置。"""

    __ui_label__ = "MiMo API"

    api_key: str = Field(default="", description="MiMo API Key")
    api_base_url: str = Field(default="https://api.xiaomimimo.com/v1", description="MiMo API 基础地址或完整接口地址")
    synthesis_mode: Literal["voiceclone", "preset"] = Field(
        default="voiceclone",
        description="voiceclone=参考音频克隆；preset=预置音色，不需要参考音频",
    )
    model: str = Field(
        default=VOICECLONE_MODEL,
        description="音色克隆使用的模型；preset 模式固定使用 mimo-v2.5-tts，忽略此项",
    )
    preset_voice: Literal["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"] = Field(
        default="冰糖",
        description="预置音色。中文：冰糖/茉莉（女）、苏打/白桦（男）；英文：Mia/Chloe（女）、Milo/Dean（男）",
    )
    audio_format: Literal["mp3", "wav"] = Field(default="mp3", description="合成音频格式")


class PluginConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    command: CommandConfig = Field(default_factory=CommandConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    mimo: MiMoConfig = Field(default_factory=MiMoConfig)


class LingTTSBot(MaiBotPlugin):
    """把 MaiBot 纯文本回复转换为 MiMo 克隆语音。"""

    config_model = PluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache_dir: Optional[Path] = None
        self._reference_lock = asyncio.Lock()
        self._synthesis_lock = asyncio.Lock()
        self._reference_signature = ""
        self._reference_uri: Optional[str] = None
        self._random_decisions: Dict[str, Tuple[float, bool]] = {}
        self._text_override_sessions: Dict[str, float] = {}
        self._bypass_counts: Dict[str, int] = {}

    async def on_load(self) -> None:
        """加载并校验参考音频。"""

        self._cache_dir = self.ctx.paths.runtime_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.config.plugin.enabled:
            self.ctx.logger.info("TTS 插件已禁用")
            return
        if self._uses_voiceclone():
            try:
                await self._ensure_reference_uri()
            except (OSError, RuntimeError, ValueError) as exc:
                self.ctx.logger.error("参考音频加载失败：%s", exc)
        allowed_users = self._allowed_command_users()
        if allowed_users:
            self.ctx.logger.info("TTS 测试命令白名单 %d 人", len(allowed_users))
        else:
            self.ctx.logger.info("TTS 测试命令白名单为空，/tts 不生效")
        self.ctx.logger.info(
            "TTS 插件已加载，触发=%s, 合成=%s, 输出=%s",
            self.config.trigger.mode,
            self.config.mimo.synthesis_mode,
            self.config.output.mode,
        )

    async def on_unload(self) -> None:
        """释放 HTTP 连接和运行时缓存。"""

        await self._close_session()
        self._cache_dir = None
        self._reference_uri = None
        self._reference_signature = ""
        self._random_decisions.clear()
        self._text_override_sessions.clear()
        self._bypass_counts.clear()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热重载后立即刷新模式、HTTP 会话和参考音频。"""

        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return

        async with self._synthesis_lock:
            await self._close_session()
            self._reference_uri = None
            self._reference_signature = ""
            self._random_decisions.clear()
            self._text_override_sessions.clear()
            self._bypass_counts.clear()
            if self.config.plugin.enabled and self._uses_voiceclone():
                try:
                    await self._ensure_reference_uri()
                except (OSError, RuntimeError, ValueError) as exc:
                    self.ctx.logger.error("热重载参考音频失败：%s", exc)
        self.ctx.logger.info(
            "TTS 配置已热重载：version=%s, 触发=%s, 合成=%s, 输出=%s, 测试命令白名单=%d",
            version,
            self.config.trigger.mode,
            self.config.mimo.synthesis_mode,
            self.config.output.mode,
            len(self._allowed_command_users()),
        )

    async def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _audio_files(self) -> List[Path]:
        raw_dir = self.config.voice.voice_dir.strip()
        if not raw_dir:
            raise ValueError("未配置 voice.voice_dir")

        voice_dir = Path(raw_dir).expanduser()
        if not voice_dir.is_dir():
            raise ValueError(f"参考音频目录不存在：{voice_dir}")

        audio_files = sorted(
            (path for path in voice_dir.iterdir() if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES),
            key=lambda path: path.name.casefold(),
        )
        if not audio_files:
            supported = ", ".join(sorted(AUDIO_SUFFIXES))
            raise ValueError(f"参考音频目录中没有支持的文件：{voice_dir}；支持 {supported}")
        return audio_files

    def _build_reference_signature(self, audio_files: List[Path]) -> str:
        file_states = [
            {
                "path": str(path.resolve()),
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
            }
            for path in audio_files
        ]
        payload = {
            "files": file_states,
            "strategy": self.config.voice.reference_strategy,
            "preferred_file": self.config.voice.preferred_reference_file.strip(),
            "max_clip_duration": self.config.voice.max_clip_duration,
            "balanced_clip_duration": self.config.voice.balanced_clip_duration,
            "silence_threshold_db": self.config.voice.silence_threshold_db,
            "sample_rate": self.config.voice.sample_rate,
            "bitrate": self.config.voice.reference_bitrate_kbps,
            "format_version": 2,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _resolve_ffmpeg(self) -> str:
        configured_path = self.config.voice.ffmpeg_path.strip()
        if configured_path:
            ffmpeg_path = Path(configured_path).expanduser()
            if not ffmpeg_path.is_file():
                raise RuntimeError(f"voice.ffmpeg_path 指向的文件不存在：{ffmpeg_path}")
            return str(ffmpeg_path)

        detected_path = shutil.which("ffmpeg")
        if not detected_path:
            raise RuntimeError("未找到 FFmpeg；请安装 FFmpeg 或配置 voice.ffmpeg_path")
        return detected_path

    def _resolve_ffprobe(self) -> str:
        """优先使用与 FFmpeg 同目录的 ffprobe，保证探测行为一致。"""

        ffmpeg_path = Path(self._resolve_ffmpeg())
        ffprobe_name = "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
        sibling_path = ffmpeg_path.with_name(ffprobe_name)
        if sibling_path.is_file():
            return str(sibling_path)

        detected_path = shutil.which("ffprobe")
        if not detected_path:
            raise RuntimeError("未找到 ffprobe；请安装包含 ffprobe 的完整 FFmpeg")
        return detected_path

    def _probe_audio(self, audio_file: Path) -> Tuple[float, float]:
        """返回音频时长与静音占比，用于挑选干净且长度合适的参考段。"""

        probe_command = [
            self._resolve_ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_file),
        ]
        try:
            probe_result = subprocess.run(
                probe_command,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ffprobe 读取 {audio_file.name} 超过 {FFMPEG_TIMEOUT_SECONDS} 秒") from exc
        if probe_result.returncode != 0:
            error = probe_result.stderr.strip() or f"退出码 {probe_result.returncode}"
            raise RuntimeError(f"ffprobe 无法读取 {audio_file.name}：{error[-500:]}")
        try:
            duration = float(probe_result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(f"ffprobe 未返回 {audio_file.name} 的有效时长") from exc
        if duration <= 0:
            raise RuntimeError(f"参考音频时长无效：{audio_file.name}")

        silence_filter = f"silencedetect=noise={self.config.voice.silence_threshold_db}dB:d=0.25"
        silence_command = [
            self._resolve_ffmpeg(),
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio_file),
            "-af",
            silence_filter,
            "-f",
            "null",
            os.devnull,
        ]
        try:
            silence_result = subprocess.run(
                silence_command,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"FFmpeg 静音检测超过 {FFMPEG_TIMEOUT_SECONDS} 秒（{audio_file.name}）") from exc
        if silence_result.returncode != 0:
            error = silence_result.stderr.strip() or f"退出码 {silence_result.returncode}"
            raise RuntimeError(f"FFmpeg 静音检测失败（{audio_file.name}）：{error[-500:]}")
        silence_seconds = sum(
            float(value)
            for value in re.findall(r"silence_duration:\s*([0-9.]+)", silence_result.stderr)
        )
        return duration, min(silence_seconds / duration, 1.0)

    def _select_reference_file(self, audio_files: List[Path]) -> Tuple[Path, float, float]:
        """按静音占比和有效长度选择最适合克隆的一段。"""

        preferred_name = self.config.voice.preferred_reference_file.strip()
        if preferred_name:
            matches = [path for path in audio_files if path.name.casefold() == preferred_name.casefold()]
            if not matches:
                raise ValueError(f"指定的参考音频不存在：{preferred_name}")
            duration, silence_ratio = self._probe_audio(matches[0])
            return matches[0], duration, silence_ratio

        candidates: List[Tuple[float, Path, float, float]] = []
        for audio_file in audio_files:
            duration, silence_ratio = self._probe_audio(audio_file)
            # 5～20 秒通常能提供足够音色信息；超出区间时施加小幅惩罚。
            duration_penalty = max(5.0 - duration, 0.0) / 10.0 + max(duration - 20.0, 0.0) / 40.0
            candidates.append((silence_ratio + duration_penalty, audio_file, duration, silence_ratio))
        _, selected_file, duration, silence_ratio = min(
            candidates,
            key=lambda item: (item[0], item[1].name.casefold()),
        )
        return selected_file, duration, silence_ratio

    def _boundary_trim_filter(self, duration: float) -> str:
        """只裁掉首尾静音，保留句子内部自然停顿和呼吸。"""

        threshold = self.config.voice.silence_threshold_db
        return (
            f"silenceremove=start_periods=1:start_duration=0.1:start_threshold={threshold}dB,"
            "areverse,"
            f"silenceremove=start_periods=1:start_duration=0.1:start_threshold={threshold}dB,"
            "areverse,"
            f"atrim=duration={duration},asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=s16:sample_rates={self.config.voice.sample_rate}:channel_layouts=mono"
        )

    @staticmethod
    def _run_ffmpeg(command: List[str], temporary_path: Path, error_prefix: str) -> None:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(f"{error_prefix}：超过 {FFMPEG_TIMEOUT_SECONDS} 秒") from exc
        if completed.returncode != 0:
            temporary_path.unlink(missing_ok=True)
            error = completed.stderr.strip() or f"退出码 {completed.returncode}"
            raise RuntimeError(f"{error_prefix}：{error[-1000:]}")
        if not temporary_path.is_file() or temporary_path.stat().st_size < 100:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg 未生成有效的参考音频")

    def _merge_reference_audio(self, audio_files: List[Path], output_path: Path) -> None:
        """按配置策略生成质量优先的 MiMo 参考音频。"""

        ffmpeg = self._resolve_ffmpeg()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(
            f"{output_path.stem}.{os.getpid()}.{time.time_ns()}.tmp{output_path.suffix}"
        )
        strategy = self.config.voice.reference_strategy

        if strategy == "best_single":
            selected_file, _, _ = self._select_reference_file(audio_files)
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(selected_file),
                "-af",
                self._boundary_trim_filter(self.config.voice.max_clip_duration),
                "-codec:a",
                "pcm_s16le",
                str(temporary_path),
            ]
            self._run_ffmpeg(command, temporary_path, "FFmpeg 生成最佳单段参考音频失败")
            temporary_path.replace(output_path)
            return

        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for audio_file in audio_files:
            command.extend(["-i", str(audio_file)])

        if strategy == "balanced":
            filters = [
                f"[{index}:a:0]{self._boundary_trim_filter(self.config.voice.balanced_clip_duration)}[a{index}]"
                for index in range(len(audio_files))
            ]
        else:
            filters = [
                (
                    f"[{index}:a:0]aformat=sample_fmts=fltp:sample_rates={self.config.voice.sample_rate}:"
                    f"channel_layouts=mono,aresample=async=1:first_pts=0[a{index}]"
                )
                for index in range(len(audio_files))
            ]
        inputs = "".join(f"[a{index}]" for index in range(len(audio_files)))
        filters.append(f"{inputs}concat=n={len(audio_files)}:v=0:a=1[outa]")
        command.extend(["-filter_complex", ";".join(filters), "-map", "[outa]"])
        if strategy == "balanced":
            command.extend(["-codec:a", "pcm_s16le"])
        else:
            command.extend(["-codec:a", "libmp3lame", "-b:a", f"{self.config.voice.reference_bitrate_kbps}k"])
        command.append(str(temporary_path))

        self._run_ffmpeg(command, temporary_path, "FFmpeg 合并参考音频失败")
        temporary_path.replace(output_path)

    async def _ensure_reference_uri(self) -> str:
        async with self._reference_lock:
            if self._cache_dir is None:
                raise RuntimeError("TTS 缓存目录尚未初始化，请确认插件已完成 on_load")

            audio_files = self._audio_files()
            signature = self._build_reference_signature(audio_files)
            if self._reference_uri is not None and signature == self._reference_signature:
                return self._reference_uri

            output_suffix = ".mp3" if self.config.voice.reference_strategy == "full_merge" else ".wav"
            output_path = self._cache_dir / f"reference-{signature}{output_suffix}"
            if not output_path.is_file():
                await asyncio.to_thread(self._merge_reference_audio, audio_files, output_path)

            reference_bytes = await asyncio.to_thread(output_path.read_bytes)
            reference_base64 = base64.b64encode(reference_bytes).decode("ascii")
            max_bytes = int(self.config.voice.max_reference_base64_mb * 1024 * 1024)
            if len(reference_base64.encode("ascii")) > max_bytes:
                raise ValueError(
                    "生成的参考音频 Base64 为 "
                    f"{len(reference_base64) / 1024 / 1024:.2f} MB，超过配置上限 "
                    f"{self.config.voice.max_reference_base64_mb:.2f} MB；请缩短参考片段或改用 best_single"
                )

            mime_type = "audio/mpeg" if output_path.suffix == ".mp3" else "audio/wav"
            self._reference_uri = f"data:{mime_type};base64,{reference_base64}"
            self._reference_signature = signature
            self._prune_reference_cache(output_path)
            self.ctx.logger.info(
                "参考音频已就绪：策略=%s, 源文件数=%d, 参考文件=%s, Base64=%.2fMB",
                self.config.voice.reference_strategy,
                len(audio_files),
                output_path.name,
                len(reference_base64) / 1024 / 1024,
            )
            return self._reference_uri

    def _prune_reference_cache(self, keep: Path) -> None:
        """只保留当前签名的参考音频，避免每次换音色都在 runtime 目录里堆积。"""

        if self._cache_dir is None:
            return
        for path in self._cache_dir.iterdir():
            if not path.is_file() or path == keep or not REFERENCE_CACHE_NAME.match(path.name):
                continue
            path.unlink(missing_ok=True)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.general.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _api_endpoint(self) -> str:
        api_url = self.config.mimo.api_base_url.strip().rstrip("/")
        if not api_url:
            raise ValueError("未配置 mimo.api_base_url")
        if api_url.endswith("/chat/completions"):
            return api_url
        return f"{api_url}/chat/completions"

    def _uses_voiceclone(self) -> bool:
        return self.config.mimo.synthesis_mode == "voiceclone"

    def _resolved_model(self) -> str:
        if not self._uses_voiceclone():
            return PRESET_MODEL
        return self.config.mimo.model.strip() or VOICECLONE_MODEL

    def _resolved_preset_voice(self) -> str:
        voice = self.config.mimo.preset_voice.strip() or "冰糖"
        if voice not in PRESET_VOICES:
            supported = "、".join(PRESET_VOICES)
            raise ValueError(f"不支持的预置音色：{voice}；可选 {supported}")
        return voice

    def _validate_credentials(self) -> None:
        api_key = self.config.mimo.api_key.strip()
        api_url = self.config.mimo.api_base_url.strip().lower()
        if not api_key:
            raise ValueError("未配置 mimo.api_key")
        if "token-plan-" in api_url and not api_key.startswith("tp-"):
            raise ValueError(
                "MiMo Token Plan 地址必须搭配 tp- 开头的 Token Plan Key；"
                "sk- Key 请改用 https://api.xiaomimimo.com/v1"
            )
        if "api.xiaomimimo.com" in api_url and api_key.startswith("tp-"):
            raise ValueError("tp- 开头的 Token Plan Key 必须搭配 Token Plan 专属 Base URL")

    @staticmethod
    def _clean_text(text: str) -> str:
        clean = re.sub(r"https?://\S+", "", text)
        clean = re.sub(r"\[CQ:[^\]]+\]", "", clean)
        return clean.strip()

    async def _synthesize(self, text: str, style: str = "") -> bytes:
        self._validate_credentials()

        clean_text = self._clean_text(text)[: self.config.general.max_text_length]
        if not clean_text:
            raise ValueError("待合成文本为空")

        async with self._synthesis_lock:
            prompt = style.strip() or self.config.voice.clone_prompt.strip()
            messages: List[Dict[str, str]] = []
            if prompt:
                messages.append({"role": "user", "content": prompt})
            messages.append({"role": "assistant", "content": clean_text})
            audio_payload: Dict[str, str] = {"format": self.config.mimo.audio_format}
            if self._uses_voiceclone():
                audio_payload["voice"] = await self._ensure_reference_uri()
            else:
                audio_payload["voice"] = self._resolved_preset_voice()
            body = {
                "model": self._resolved_model(),
                "messages": messages,
                "audio": audio_payload,
            }
            headers = {
                "api-key": self.config.mimo.api_key,
                "Content-Type": "application/json",
            }
            session = await self._get_session()
            try:
                async with session.post(self._api_endpoint(), json=body, headers=headers) as response:
                    response_text = await response.text()
                    if response.status != 200:
                        raise RuntimeError(f"MiMo API 返回 HTTP {response.status}：{response_text[:1000]}")
                    try:
                        response_data = json.loads(response_text)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("MiMo API 返回了非 JSON 响应") from exc
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"MiMo API 请求超过 {self.config.general.timeout} 秒") from exc
            except aiohttp.ClientError as exc:
                raise RuntimeError(f"MiMo API 网络请求失败：{exc}") from exc

        try:
            audio_base64 = response_data["choices"][0]["message"]["audio"]["data"]
            audio = base64.b64decode(audio_base64, validate=True)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("MiMo API 响应中缺少有效的 choices[0].message.audio.data") from exc
        if len(audio) < 100:
            raise RuntimeError("MiMo API 返回的音频数据过短")
        self.ctx.logger.info(
            "MiMo 合成完成：模型=%s, 音频=%dKB, 文本=%s",
            self._resolved_model(),
            len(audio) // 1024,
            clean_text[:40],
        )
        return audio

    @staticmethod
    def _stream_id(message: Dict[str, Any], kwargs: Dict[str, Any]) -> str:
        return str(kwargs.get("stream_id") or message.get("session_id") or "").strip()

    @staticmethod
    def _plain_text_message(message: Dict[str, Any]) -> Optional[str]:
        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list) or not raw_message:
            return None
        texts: List[str] = []
        for item in raw_message:
            if not isinstance(item, dict):
                return None
            item_type = item.get("type")
            # 引用段是发送前才插入的，不能因此把整条回复当成非文本。
            if item_type == "reply":
                continue
            if item_type != "text":
                return None
            data = item.get("data")
            if isinstance(data, dict):
                data = data.get("text") or ""
            texts.append(str(data or ""))
        if not texts:
            return None
        text = "".join(texts).strip()
        return text or None

    @staticmethod
    def _is_plugin_notice(text: str) -> bool:
        """命令回执不能再送去合成，否则会把「语音已发送」又读一遍。"""

        stripped = text.strip()
        return stripped in PLUGIN_NOTICE_EXACT or stripped.startswith("语音合成失败：")

    def _text_override_active(self, stream_id: str) -> bool:
        """检查当前聊天流是否仍处于 LLM 选择的文字回复会话窗口。"""

        expires_at = self._text_override_sessions.get(stream_id, 0.0)
        if expires_at >= time.monotonic():
            return True
        self._text_override_sessions.pop(stream_id, None)
        return False

    def _random_voice_enabled(self, stream_id: str) -> bool:
        now = time.monotonic()
        cached = self._random_decisions.get(stream_id)
        if cached is not None and cached[0] >= now:
            return cached[1]

        import random

        enabled = random.random() < self.config.trigger.probability
        self._random_decisions[stream_id] = (now + self.config.trigger.random_decision_ttl, enabled)
        if len(self._random_decisions) > 1000:
            self._random_decisions = {
                key: value for key, value in self._random_decisions.items() if value[0] >= now
            }
        return enabled

    def _enter_bypass(self, stream_id: str) -> None:
        self._bypass_counts[stream_id] = self._bypass_counts.get(stream_id, 0) + 1

    def _exit_bypass(self, stream_id: str) -> None:
        remaining = self._bypass_counts.get(stream_id, 0) - 1
        if remaining <= 0:
            self._bypass_counts.pop(stream_id, None)
        else:
            self._bypass_counts[stream_id] = remaining

    def _is_bypassed(self, stream_id: str) -> bool:
        return self._bypass_counts.get(stream_id, 0) > 0

    def _should_voice(self, stream_id: str) -> bool:
        """当前这条纯文本是否要配语音。文字豁免窗口内返回 False。"""

        if self._is_bypassed(stream_id):
            return False
        mode = self.config.trigger.mode
        if mode == "llm_trigger":
            return not self._text_override_active(stream_id)
        if mode == "voice_only":
            return True
        if mode == "random":
            return self._random_voice_enabled(stream_id)
        return False

    def _outbound_candidate(
        self,
        message: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
    ) -> Optional[Tuple[Dict[str, Any], str, str]]:
        if not self.config.plugin.enabled:
            return None
        outbound = message if isinstance(message, dict) else {}
        text = self._plain_text_message(outbound)
        if text is None:
            return None
        stream_id = self._stream_id(outbound, kwargs)
        if not stream_id:
            self.ctx.logger.error("出站 TTS 无法获取真实聊天流 session_id")
            return None
        return outbound, stream_id, text

    @HookHandler(
        "send_service.before_send",
        name="ling_tts_before_send",
        description="voice_only 时把纯文本原位替换为语音；text_and_voice 时保留原文",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=300000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def convert_outbound_text(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """在出站消息真正发送前，按输出模式决定是替换成语音还是放行文字。"""

        candidate = self._outbound_candidate(message, kwargs)
        if candidate is None:
            return None
        outbound, stream_id, text = candidate
        if self._is_plugin_notice(text):
            return None
        if self.config.trigger.mode == "llm_trigger" and self._text_override_active(stream_id):
            self.ctx.logger.info("LLM 已选择文字回复，保留聊天流 %s 的文字及引用关系", stream_id)
            return None
        # QQ 不投递「文字段 + 语音段」或「引用段 + 语音段」。文字另走原消息，语音在 after_send 单发。
        if self.config.output.mode == "text_and_voice":
            return None
        if not self._should_voice(stream_id):
            return None
        if not self._clean_text(text):
            self.ctx.logger.info("没有可朗读的文本，保留聊天流 %s 的原文", stream_id)
            return None

        try:
            audio = await self._synthesize(text)
        except (OSError, RuntimeError, ValueError) as exc:
            self.ctx.logger.error("出站文字转语音失败，已阻止文字发送：%s", exc)
            return {"action": "abort"}

        audio_base64 = base64.b64encode(audio).decode("ascii")
        outbound["raw_message"] = [
            {
                "type": "voice",
                "data": "",
                "hash": hashlib.sha256(audio).hexdigest(),
                "binary_data_base64": audio_base64,
            }
        ]
        outbound["processed_plain_text"] = text
        # QQ/NapCat 会接受“引用段 + record 段”的请求并返回成功回执，
        # 但 QQ 实际不会投递该组合消息。语音回复必须退化为不带引用的独立语音。
        had_reply = bool(kwargs.get("set_reply")) or bool(outbound.get("reply_to"))
        outbound["reply_to"] = None
        kwargs["message"] = outbound
        kwargs["set_reply"] = False
        kwargs["reply_message_id"] = ""
        if had_reply:
            self.ctx.logger.info("QQ 语音不支持引用组合，已移除聊天流 %s 的引用段", stream_id)
        self.ctx.logger.info("已将聊天流 %s 的纯文本回复替换为语音", stream_id)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "send_service.after_send",
        name="ling_tts_after_send",
        description="文字发送成功后另发一条语音，避免和文字挤在同一条 QQ 消息里",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=300000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def send_companion_voice(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        """text_and_voice：原文已经发出后，再补一条不带引用的语音。"""

        if self.config.output.mode != "text_and_voice":
            return None
        if not bool(kwargs.get("sent")):
            return None
        candidate = self._outbound_candidate(message, kwargs)
        if candidate is None:
            return None
        _outbound, stream_id, text = candidate
        if self._is_plugin_notice(text) or not self._clean_text(text):
            return None
        if not self._should_voice(stream_id):
            return None

        try:
            audio = await self._synthesize(text)
            sent = await self._send_voice(audio, stream_id, text, remember_history=False)
        except Exception as exc:
            self.ctx.logger.error("补发语音失败，文字已保留：%s", exc)
            return None
        if sent:
            self.ctx.logger.info("已为聊天流 %s 在文字之外补发语音", stream_id)
        else:
            self.ctx.logger.error("补发语音未被接受，文字已保留")
        return None

    @HookHandler(
        "maisaka.planner.before_request",
        name="ling_tts_planner_mode",
        description="根据热重载后的触发模式控制 LLM 语音工具是否可见",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def configure_planner_tools(self, **kwargs: Any) -> Dict[str, Any]:
        """仅在 LLM 模式提供一次性文字许可工具，并注入模式说明。"""

        tool_definitions = kwargs.get("tool_definitions")
        if isinstance(tool_definitions, list) and self.config.trigger.mode != "llm_trigger":
            kwargs["tool_definitions"] = [
                item
                for item in tool_definitions
                if self._tool_definition_name(item) != TEXT_REPLY_TOOL_NAME
            ]
        elif self.config.trigger.mode == "llm_trigger":
            messages = kwargs.get("messages")
            if isinstance(messages, list):
                messages.append({"role": "system", "content": self._planner_instruction()})
                kwargs["messages"] = messages
        return {"action": "continue", "modified_kwargs": kwargs}

    def _planner_instruction(self) -> str:
        if self.config.output.mode == "text_and_voice":
            return (
                "当前回复会同时给出文字和语音：直接调用 reply，插件会保留文字并额外发送一条语音。"
                "仅当内容不适合朗读（代码、网址、表格、长列表、精确格式，或用户明确只要文字）时，"
                "先调用 ling_text_reply，再调用 reply；当前回复会话只发文字、不发语音，并保留引用。"
                "不要仅因为回复引用了消息就选择文字。"
            )
        return (
            "当前回复模式为只发语音：通常直接调用 reply，插件会自动把纯文本回复转换为语音。"
            "仅当内容确实更适合文字展示（例如代码、网址、表格、长列表、精确格式，或用户明确要求文字）时，"
            "先调用 ling_text_reply，再调用 reply；当前回复会话中的文字消息都会保留文字和引用关系。"
            "不要仅因为回复引用了消息就选择文字，也不要在一次回复中同时发送文字和语音。"
        )

    @staticmethod
    def _tool_definition_name(definition: Any) -> str:
        if not isinstance(definition, dict):
            return ""
        function = definition.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "").rsplit(".", 1)[-1]
        return str(definition.get("name") or "").rsplit(".", 1)[-1]

    async def _send_voice(
        self,
        audio: bytes,
        stream_id: str,
        processed_text: str,
        *,
        remember_history: bool,
    ) -> bool:
        audio_base64 = base64.b64encode(audio).decode("ascii")
        sent = await self.ctx.send.custom(
            "voice",
            audio_base64,
            stream_id,
            processed_plain_text=processed_text,
            sync_to_maisaka_history=remember_history,
            maisaka_source_kind="tool_voice",
        )
        return bool(sent)

    @Tool(
        TEXT_REPLY_TOOL_NAME,
        brief_description="让当前回复会话只发文字、不附带语音",
        detailed_description=(
            "默认不要调用。"
            "仅当代码、网址、表格、长列表、精确格式或用户明确只要文字时调用，"
            "然后调用普通 reply；当前回复会话不再附加或替换为语音，并可正常引用消息。"
        ),
        activation_type=ActivationType.ALWAYS,
        parameters=[
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="简要说明为什么本次内容更适合文字展示",
                required=False,
                default="",
            ),
        ],
    )
    async def allow_text_reply(
        self,
        reason: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """允许当前聊天流在短时回复会话窗口内绕过语音转换。"""

        if not self.config.plugin.enabled:
            return {"success": False, "content": "TTS 插件未启用"}
        if self.config.trigger.mode != "llm_trigger":
            return {"success": False, "content": "当前不是语音优先模式，请直接使用普通 reply"}

        stream_id = str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()
        if not stream_id:
            return {"success": False, "content": "无法获取真实聊天流 session_id"}

        self._text_override_sessions[stream_id] = time.monotonic() + self.config.trigger.text_override_ttl
        self.ctx.logger.info("LLM 为聊天流 %s 选择文字回复：%s", stream_id, reason.strip() or "未说明")
        return {
            "success": True,
            "content": "当前回复会话已切换为文字；现在请调用 reply 完成回复",
            "method": "text",
        }

    def _allowed_command_users(self) -> set[str]:
        return {str(item).strip() for item in self.config.command.allowed_user_ids if str(item).strip()}

    @staticmethod
    def _message_user_id(message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        info = message.get("message_info") or {}
        if not isinstance(info, dict):
            return ""
        user = info.get("user_info") or {}
        if not isinstance(user, dict):
            return ""
        return str(user.get("user_id") or "").strip()

    def _command_sender_id(self, user_id: str, kwargs: Dict[str, Any]) -> str:
        sender = str(user_id or kwargs.get("user_id") or "").strip()
        if sender:
            return sender
        return self._message_user_id(kwargs.get("message"))

    @Command(
        "ling_tts_cmd",
        description="手动将文字转换为语音",
        pattern=r"^/(?:tts|voice|mimo)\s+(?P<text>.+)$",
    )
    async def cmd_tts(
        self,
        stream_id: str = "",
        user_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        """处理手动 TTS 测试命令。白名单为空或不包含发送者时不生效。"""

        sender = self._command_sender_id(user_id, kwargs)
        allowed_users = self._allowed_command_users()
        if sender not in allowed_users:
            self.ctx.logger.info("拒绝 /tts：用户 %s 不在白名单", sender or "未知")
            return False, "测试命令未对你开放", True

        text = str((matched_groups or {}).get("text") or "").strip()
        if not stream_id:
            return False, "缺少真实聊天流 session_id", True
        if not text:
            return False, "用法：/tts <文本>", True

        clean_text = self._clean_text(text)[: self.config.general.max_text_length]
        send_text = self.config.output.mode == "text_and_voice"
        self._enter_bypass(stream_id)
        try:
            if send_text:
                await self.ctx.send.text(
                    clean_text,
                    stream_id,
                    processed_plain_text=clean_text,
                    sync_to_maisaka_history=True,
                    maisaka_source_kind="tool_text",
                )
            audio = await self._synthesize(clean_text)
            sent = await self._send_voice(audio, stream_id, clean_text, remember_history=not send_text)
        except Exception as exc:
            self.ctx.logger.error("手动 TTS 失败：%s", exc)
            return False, f"语音合成失败：{exc}", True
        finally:
            self._exit_bypass(stream_id)
        if not sent:
            return False, "语音发送失败", True
        return (True, "文字和语音已发送", True) if send_text else (True, "语音已发送", True)


def create_plugin() -> LingTTSBot:
    """创建插件实例。"""

    return LingTTSBot()
