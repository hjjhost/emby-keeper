import asyncio
from datetime import datetime, timedelta
import random
import re
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from enum import Flag, auto
import string
import time
from typing import Iterable, List, Optional, Tuple, Union

from loguru import logger
from pyrogram import filters
from pyrogram.errors import (
    UsernameNotOccupied,
    FloodWait,
    UsernameInvalid,
    ChannelInvalid,
    ChannelPrivate,
    MessageIdInvalid,
    DataInvalid,
)
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from pyrogram.raw.functions.account import GetNotifySettings
from pyrogram.raw.types import PeerNotifySettings, InputNotifyPeer
from thefuzz import fuzz

from embykeeper import __name__ as __product__
from embykeeper.ocr import CharRange, OCRService
from embykeeper.runinfo import RunContext
from embykeeper.utils import show_exception, to_iterable, format_timedelta_human, AsyncCountPool
from embykeeper.config import config
from embykeeper.runinfo import RunStatus
from embykeeper.telegram.pyrogram import Client
from embykeeper.telegram.link import Link

__ignore__ = True

logger = logger.bind(scheme="telechecker")

default_keywords = {
    "account_fail": (
        "拉黑",
        "黑名单",
        "冻结",
        "未找到用户",
        "无资格",
        "退出群",
        "退群",
        "加群",
        "加入群聊",
        "请先关注",
        "请先加入",
        "請先加入",
        "未注册",
        "先注册",
        "不存在",
        "不在群组中",
        "你有号吗",
    ),
    "too_many_tries_fail": ("已尝试", "过多"),
    "checked": ("只能", "已经", "过了", "签过", "明日再来", "重复签到"),
    "fail": ("失败", "错误", "超时"),
    "success": ("成功", "通过", "完成", "获得"),
}


class MessageType(Flag):
    IGNORE = auto()
    TEXT = auto()
    CAPTION = auto()
    CAPTCHA = auto()
    ANSWER = auto()


class BaseBotCheckin(ABC):
    """基础签到类."""

    name: str = None

    def __init__(
        self,
        client: Client,
        context: RunContext = None,
        retries=None,
        timeout=None,
        config: dict = {},
    ):
        """
        基础签到类.
        参数:
            client: Pyrogram 客户端
            context: 运行时上下文
            retries: 最大重试次数
            timeout: 签到超时时间
            config: 当前签到器的特定配置
        """
        self.client = client
        self.ctx = context or RunContext.prepare()

        self._retries = retries
        self._timeout = timeout

        self.config = config
        self.finished = asyncio.Event()  # 签到完成事件
        self.log = self.ctx.bind_logger(logger.bind(name=self.name, username=client.me.full_name))  # 日志组件

        self._task = None  # 主任务

    @property
    def retries(self):
        return self._retries or config.checkiner.retries

    @property
    def timeout(self):
        return self._timeout or config.checkiner.timeout

    async def _start(self):
        """签到器的入口函数的错误处理外壳."""
        try:
            self.client.stop_handlers.append(self.stop)
            self._task = asyncio.create_task(self.start())
            return await self._task
        except Exception as e:
            if config.nofail:
                self.log.warning(f"初始化异常错误, 签到器将停止.")
                show_exception(e, regular=False)
                return self.ctx.finish(RunStatus.ERROR, "异常错误")
            else:
                raise
        finally:
            self.client.stop_handlers.remove(self.stop)
            self._task = None

    @abstractmethod
    async def start(self) -> RunContext:
        pass

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class BotCheckin(BaseBotCheckin):
    """签到类, 用于回复模式签到."""

    group_pool = AsyncCountPool(base=2000)
    interval_pool = {}

    # fmt: off
    name: str = None  # 签到器的名称
    bot_username: Union[int, str] = None  # Bot 的 UserID 或 用户名 (不带 @ 或 https://t.me/)
    bot_checkin_cmd: Union[str, List[str]] = ["/checkin"]  # Bot 依次执行的签到命令
    bot_send_interval: int = 3  # 签到命令间等待的秒数
    bot_use_captcha = True # 当 Bot 返回图片时, 识别验证码并调用 on_captcha
    bot_checkin_caption_pat: str = None  # 当 Bot 返回图片时, 仅当符合该 regex 才识别为验证码, 置空不限制
    bot_text_ignore: Union[str, List[str]] = []  # 当含有列表中的关键词, 即忽略该消息, 置空不限制
    ocr: Optional[str] = None # OCR 模型, None = 默认模型, str = 自定义模型
    bot_captcha_char_range: Optional[Union[CharRange, str]] = None # OCR 字符范围, 仅当默认模型可用, None = 默认范围, OCRRanges = 预定义范围, str = 自定义范围
    bot_captcha_len: Union[int, Iterable[int]] = []  # 验证码长度的可能范围, 例如 [1, 2, 3], 置空不限制
    bot_success_pat: str = r"(\d+)[^\d]*(\d+)"  # 当接收到成功消息后, 从消息中提取数字的模式
    bot_retry_wait: int = 2  # 失败时等待的秒数
    bot_use_history: int = None  # 首先尝试识别历史记录中最后一个验证码图片, 最多识别 N 条, 置空禁用
    bot_allow_from_scratch: bool = False  # 允许从未聊天情况下启动
    bot_success_keywords: Union[str, List[str]] = []  # 成功时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_checked_keywords: Union[str, List[str]] = []  # 今日已签到时检测的关键词, 置空使用内置关键词表
    bot_account_fail_keywords: Union[str, List[str]] = []  # 账户错误将退出时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_too_many_tries_fail_keywords: Union[str, List[str]] = []  # 过多尝试将退出时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_fail_keywords: Union[str, List[str]] = []  # 签到错误将重试时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    chat_name: str = None  # 在群聊中向机器人签到
    additional_auth: List[str] = []  # 额外认证要求
    max_retries = None  # 验证码错误或网络错误时最高重试次数 (默认无限)
    checked_retries = None # 今日已签到时最高重试次数 (默认不重试)
    init_first: bool = False  # 先执行自定义初始化函数, 再进行加入群组分析
    # fmt: on

    @property
    def valid_retries(self):
        if self.max_retries:
            return min(self.retries, self.max_retries)
        else:
            return self.retries

    def __init__(self, *args, instant=False, **kw):
        super().__init__(*args, **kw)
        self.current_retries = 0  # 当前重试次数
        self._waiting = {}  # 当前等待的消息
        self._first_waiting = False  # 是否在等待首个消息
        self._handler_tasks = set()  # 存储所有正在运行的 message_handler 任务
        self._pending_click_captcha_message: Optional[Message] = None
        if instant:
            self.checked_retries = None

    def get_filter(self):
        """设定要签到的目标."""
        filter = filters.all
        if self.bot_username:
            filter = filter & filters.user(self.bot_username)
        if self.chat_name:
            filter = filter & filters.chat(self.chat_name)
        else:
            filter = filter & filters.private
        return filter

    def get_handlers(self):
        """设定要监控的更新的类型."""
        return [
            MessageHandler(self._message_handler, self.get_filter()),
            EditedMessageHandler(self._message_handler, self.get_filter()),
        ]

    @asynccontextmanager
    async def listener(self):
        """执行监控上下文."""
        group = await self.group_pool.append(self)
        handlers = self.get_handlers()
        for h in handlers:
            await self.client.add_handler(h, group=group)
        try:
            yield
        finally:
            # 取消所有正在运行的message_handler任务
            for task in self._handler_tasks:
                task.cancel()
            # 等待所有任务完成
            if self._handler_tasks:
                await asyncio.gather(*self._handler_tasks, return_exceptions=True)
            # 移除handlers
            for h in handlers:
                try:
                    await self.client.remove_handler(h, group=group)
                except ValueError:
                    pass

    async def start(self):
        """签到器的入口函数."""

        self.ctx.start(RunStatus.INITIALIZING)

        if self.init_first:
            if not await self.init():
                self.log.warning(f"初始化错误.")
                return self.ctx.finish(RunStatus.FAIL, "初始化错误")

        if (not self.chat_name) and (not self.bot_username):
            raise ValueError("未指定 chat_name 或 bot_username")
        ident = self.chat_name or self.bot_username

        while True:
            try:
                chat = await self.client.get_chat(ident)
            except (UsernameNotOccupied, UsernameInvalid, ChannelInvalid, ChannelPrivate) as e:
                self.log.warning(f'初始化错误: 会话 "{ident}" 已不存在.')
                return self.ctx.finish(RunStatus.IGNORE, "会话已不存在")
            except KeyError as e:
                self.log.info(f"初始化错误: 无法访问, 您可能已被封禁: {e}.")
                show_exception(e)
                return self.ctx.finish(RunStatus.FAIL, "无法访问会话")
            except FloodWait as e:
                if e.value < 360:
                    self.log.info(f"初始化信息: Telegram 要求等待 {e.value} 秒.")
                    await asyncio.sleep(e.value)
                else:
                    self.log.info(
                        f"初始化信息: Telegram 要求等待 {e.value} 秒, 您可能操作过于频繁, 签到器将停止."
                    )
                    return self.ctx.finish(RunStatus.FAIL, "操作过于频繁")
            else:
                break

        if self.chat_name:
            self.chat_name = chat.username or chat.id

        is_archived = chat.folder_id == 1

        if await self.client.get_chat_history_count(chat.id) == 0:
            if not self.bot_allow_from_scratch:
                self.log.debug(f'跳过签到: 从未与 "{ident}" 交流.')
                return self.ctx.finish(RunStatus.IGNORE, "从未与该会话交流")

        while True:
            if self.additional_auth:
                for a in self.additional_auth:
                    if not await Link(self.client).auth(a, log_func=self.log.info):
                        return self.ctx.finish(RunStatus.IGNORE, "需要额外认证")

            if not self.init_first:
                if not await self.init():
                    self.log.warning(f"初始化错误.")
                    return self.ctx.finish(RunStatus.FAIL, "初始化错误")

            specs = []
            if self.bot_username:
                try:
                    bot = await self.client.get_users(self.bot_username)
                except IndexError:
                    self.log.warning(f"初始化错误: 用户名 {self.bot_username} 不存在或不是有效用户.")
                    return self.ctx.finish(RunStatus.FAIL, "初始化错误")
                specs.append(f"[green]{bot.full_name}[/] [gray50](@{bot.username})[/]")
            if chat.title:
                specs.append(f"[green]{chat.title}[/] [gray50](@{chat.username})[/]")
            else:
                specs.append(f"[green]@{chat.username}[/]")
            if specs:
                self.log.info(f"开始执行签到: {' @ '.join(specs)}.")

            if not self.chat_name:
                self.log.debug(f"[gray50]禁用提醒 {self.timeout} 秒: {bot.username}[/]")
                peer = InputNotifyPeer(peer=await self.client.resolve_peer(ident))
                try:
                    settings: PeerNotifySettings = await self.client.invoke(GetNotifySettings(peer=peer))
                except FloodWait:
                    self.log.debug(f"[gray50]获取当前提醒设置因访问超限而失败: {bot.username}[/]")
                    old_mute_until = 0
                else:
                    old_mute_until = settings.mute_until
                try:
                    await self.client.mute_chat(ident, time.time() + self.timeout + 10)
                except FloodWait:
                    self.log.debug(f"[gray50]设置禁用提醒因访问超限而失败: {bot.username}[/]")

            self.ctx.status = RunStatus.RUNNING

            try:
                async with self.listener():
                    try:
                        if self.bot_use_history is None:
                            await self.send_checkin()
                        elif not await self.walk_history(self.bot_use_history):
                            await self.send_checkin()
                        await asyncio.wait_for(self.finished.wait(), self.timeout)
                    finally:
                        try:
                            if await asyncio.wait_for(self.cleanup(), 3):
                                self.log.debug(f"[gray50]执行清理成功: {ident}[/]")
                            else:
                                self.log.debug(f"[gray50]执行清理失败: {ident}[/]")
                        except asyncio.TimeoutError:
                            self.log.debug(f"[gray50]执行清理失败: {ident}[/]")
                        if is_archived:
                            self.log.debug(f"[gray50]将会话重新归档: {ident}[/]")
                            try:
                                if await asyncio.wait_for(chat.archive(), 3):
                                    self.log.debug(f"[gray50]重新归档成功: {ident}[/]")
                            except asyncio.TimeoutError:
                                self.log.debug(f"[gray50]归档失败: {ident}[/]")
                            except FloodWait:
                                self.log.debug(f"[gray50]归档因访问超限而失败: {ident}[/]")
                        if not self.chat_name:
                            self.log.debug(f"[gray50]将会话设为已读: {ident}[/]")
                            try:
                                if await asyncio.wait_for(self.client.read_chat_history(ident), 3):
                                    self.log.debug(f"[gray50]设为已读成功: {ident}[/]")
                            except asyncio.TimeoutError:
                                self.log.debug(f"[gray50]设为已读失败: {ident}[/]")
                            except FloodWait:
                                self.log.debug(f"[gray50]设为已读因访问超限而失败: {ident}[/]")
            except OSError as e:
                self.log.warning(f'初始化错误: "{e}", 签到器将停止.')
                show_exception(e)
                return self.ctx.finish(RunStatus.FAIL, f"初始化错误: {e}")
            except asyncio.TimeoutError:
                pass
            finally:
                if not self.chat_name:
                    if old_mute_until:
                        try:
                            await asyncio.wait_for(self.client.mute_chat(ident, until=old_mute_until), 3)
                        except asyncio.TimeoutError:
                            self.log.debug(f"[gray50]重新设置通知设置失败: {ident}[/]")
                        except FloodWait:
                            self.log.debug(f"[gray50]重新设置通知设置因访问超限而失败: {ident}[/]")
                        else:
                            self.log.debug(f"[gray50]重新设置通知设置成功: {ident}[/]")
            if not self.finished.is_set():
                self.log.warning("无法在时限内完成签到.")
                return self.ctx.finish(RunStatus.FAIL, "无法在时限内完成签到")
            elif self.current_retries <= self.valid_retries:
                if (
                    self.ctx.status == RunStatus.NONEED
                ):  # 已签到的情况, 如果设置了 checked_retries, 则返回重计划请求
                    if self.checked_retries:
                        if self.ctx.reschedule and self.ctx.reschedule > self.checked_retries:
                            return self.ctx.finish(RunStatus.NONEED)
                        else:
                            now = datetime.now()
                            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                                days=1
                            )
                            max_sleep = midnight - now
                            sleep = timedelta(hours=min((self.ctx.reschedule or 0 + 1) * 1, 6))
                            if sleep > max_sleep:
                                return self.ctx.finish(RunStatus.NONEED)
                            else:
                                self.log.info(f"今日已签到, 即将在 {format_timedelta_human(sleep)} 后重试.")
                                self.ctx.next_time = now + sleep
                                return self.ctx.finish(RunStatus.RESCHEDULE, "等待重新尝试签到")
                return self.ctx.finish()
            else:
                return self.ctx.finish(RunStatus.FAIL, "重试次数超限")

    async def init(self):
        """可重写的初始化函数, 在读取聊天后运行, 在执行签到前运行, 返回 False 将视为初始化错误."""
        return True

    async def cleanup(self):
        """可重写的签到结束后的清理函数, 执行签到成功或失败后运行, 返回 False 将视为清理错误."""
        return True

    async def walk_history(self, limit=0):
        """处理 limit 条历史消息, 并检测是否有验证码."""
        try:
            async for m in self.client.get_chat_history(self.chat_name or self.bot_username, limit=limit):
                if MessageType.CAPTCHA in self.message_type(m):
                    await self.on_photo(m)
                    return True
            return False
        except Exception as e:
            self.log.warning("读取历史消息失败, 将不再读取历史消息.")
            show_exception(e)
            return False

    async def send(self, cmd):
        """向机器人发送命令."""
        if self.chat_name:
            return await self.client.send_message(self.chat_name, cmd)
        else:
            return await self.client.send_message(self.bot_username, cmd)

    async def send_checkin(self, retry=False):
        """发送签到命令, 或依次发送签到命令序列."""
        cmds = to_iterable(self.bot_checkin_cmd)
        for i, cmd in enumerate(cmds):
            if retry and not i:
                await asyncio.sleep(self.bot_retry_wait)
            if i < len(cmds):
                await asyncio.sleep(self.bot_send_interval)
            self._first_waiting = True
            await self.send(cmd)

    async def _message_handler(self, client: Client, message: Message):
        """消息处理入口函数的错误处理外壳."""
        try:
            if self._first_waiting:
                self._first_waiting = False
                message.is_first_response = True
            # 创建新的任务并存储
            task = asyncio.create_task(self.message_handler(client, message))
            self._handler_tasks.add(task)
            await task
        except OSError as e:
            self.log.info(f'发生错误: "{e}", 正在重试.')
            show_exception(e)
            await self.retry()
            message.continue_propagation()
        except asyncio.CancelledError:
            task.cancel()
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            finally:
                raise
        except Exception as e:
            if not config.nofail:
                await self.fail()
                raise
            else:
                self.log.warning(f"发生错误, 签到器将停止.")
                show_exception(e, regular=False)
                await self.fail()
                message.continue_propagation()
        else:
            message.continue_propagation()
        finally:
            if task in self._handler_tasks:
                self._handler_tasks.remove(task)

    async def message_handler(self, client: Client, message: Message, type=None):
        """消息处理入口函数."""
        text = message.text or message.caption
        if text:
            for p, k in self._waiting.items():
                if re.search(p, text):
                    k.set()
                    self._waiting[p] = message
        if self.is_click_captcha_prompt(message):
            self.remember_click_captcha_message(message)
            return
        type = type or self.message_type(message)
        if type:
            if MessageType.TEXT in type:
                await self.on_text(message, message.text)
            if MessageType.CAPTION in type:
                await self.on_text(message, message.caption)
            if MessageType.CAPTCHA in type:
                await self.on_photo(message)

    def message_type(self, message: Message):
        """分析传入消息的类型为验证码或文字."""
        if message.photo:
            if self._pending_click_captcha_message:
                return MessageType.CAPTCHA
            if message.caption:
                if self.bot_use_captcha:
                    if self.bot_checkin_caption_pat:
                        if re.search(self.bot_checkin_caption_pat, message.caption):
                            return MessageType.CAPTCHA
                        else:
                            return MessageType.CAPTION
                    else:
                        return MessageType.CAPTCHA
                else:
                    return MessageType.CAPTION
            else:
                if self.bot_use_captcha:
                    return MessageType.CAPTCHA
                else:
                    return MessageType.IGNORE
        elif message.text:
            return MessageType.TEXT
        else:
            return MessageType.IGNORE

    async def on_photo(self, message: Message):
        """分析传入的验证码图片并返回验证码."""
        try:
            captcha = await self.recognize_captcha_text(message)
        except asyncio.TimeoutError:
            self.log.info("签到失败: 验证码识别失败, 正在重试.")
            await self.retry()
            return

        if captcha:
            self.log.debug(f"[gray50]接收验证码: {captcha}.[/]")
            if self.bot_captcha_len and len(captcha) not in to_iterable(self.bot_captcha_len):
                self.log.info(f"签到失败: 验证码低于设定长度, 正在重试.")
                await self.retry()
            else:
                await asyncio.sleep(random.uniform(2, 4))
                await self.on_captcha(message, captcha)
        else:
            self.log.info(f"签到失败: 接收到空验证码, 正在重试.")
            await self.retry()

    async def recognize_captcha_text(self, message: Message) -> str:
        """使用本地 OCRService 识别验证码图片并返回清理后的文本."""
        data = await self.client.download_media(message, in_memory=True)
        char_range = self.get_click_captcha_char_range() or self.bot_captcha_char_range
        ocr = await OCRService.get(
            ocr_name=self.ocr,
            char_range=char_range,
        )

        with ocr:
            is_gif = getattr(data, "name", "").endswith(".gif")
            ocr_text = await ocr.run(data, gif=is_gif)
            if not ocr_text:
                return ""
            return ocr_text.translate(str.maketrans("", "", string.punctuation)).replace(" ", "")

    def get_button_texts(self, message: Message) -> List[str]:
        """获取消息上的所有按钮文本."""
        reply_markup = message.reply_markup
        if isinstance(reply_markup, InlineKeyboardMarkup):
            return [k.text for r in reply_markup.inline_keyboard for k in r if k.text]
        if isinstance(reply_markup, ReplyKeyboardMarkup):
            return [k.text for r in reply_markup.keyboard for k in r if k.text]
        return []

    def is_click_captcha_prompt(self, message: Message) -> bool:
        """判断消息是否是“看图点击按钮”类验证码提示."""
        text = message.text or message.caption or ""
        if not text or not message.reply_markup:
            return False
        buttons = self.get_button_texts(message)
        if not buttons:
            return False
        prompt_keywords = (
            "请点击",
            "請點擊",
            "图片中显示",
            "圖片中顯示",
            "图中",
            "圖中",
            "签到验证",
            "簽到驗證",
            "人机验证",
            "人機驗證",
            "数字",
            "數字",
        )
        if not any(keyword in text for keyword in prompt_keywords):
            return False
        return any(not self._is_back_button(button) for button in buttons)

    def remember_click_captcha_message(self, message: Message):
        """缓存带按钮的验证消息, 等待随后到来的图片验证码."""
        self._pending_click_captcha_message = message
        self.log.debug("已记录图片点击验证码按钮消息, 等待验证码图片.")

    @staticmethod
    def _normalize_captcha_choice(value: str) -> str:
        return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).lower()

    @staticmethod
    def _is_back_button(value: str) -> bool:
        return any(keyword in str(value or "") for keyword in ("返回", "取消", "退出", "关闭", "關閉"))

    def get_click_captcha_choices(self, message: Message) -> List[Tuple[str, str]]:
        choices = [
            (button, self._normalize_captcha_choice(button))
            for button in self.get_button_texts(message)
            if not self._is_back_button(button)
        ]
        return [(button, normalized) for button, normalized in choices if normalized]

    def get_click_captcha_char_range(self) -> Optional[str]:
        if not self._pending_click_captcha_message:
            return None
        choices = self.get_click_captcha_choices(self._pending_click_captcha_message)
        normalized = [normalized for _, normalized in choices]
        if normalized and all(len(choice) == 1 for choice in normalized):
            return "".join(dict.fromkeys("".join(normalized)))
        return None

    async def click_captcha_button(self, message: Message, captcha: str) -> bool:
        """用 OCR 结果点击同消息或已缓存消息上的按钮."""
        target = self._pending_click_captcha_message or (message if message.reply_markup else None)
        if not target:
            return False
        captcha_norm = self._normalize_captcha_choice(captcha)
        if not captcha_norm:
            return False
        choices = self.get_click_captcha_choices(target)
        if not choices:
            return False

        single_char_matches = [
            button for button, normalized in choices if len(normalized) == 1 and normalized in captcha_norm
        ]
        exact = [button for button, normalized in choices if normalized == captcha_norm]
        contains = [button for button, normalized in choices if captcha_norm in normalized]
        if exact:
            button = exact[0]
        elif len(single_char_matches) == 1:
            button = single_char_matches[0]
        elif contains:
            button = contains[0]
        else:
            button, score = max(
                ((button, fuzz.ratio(normalized, captcha_norm)) for button, normalized in choices),
                key=lambda item: item[1],
            )
            if score < 75:
                self.log.info(f'未能找到对应 "{captcha}" 的验证码按钮, 正在重试.')
                return False

        try:
            await target.click(button)
        except (TimeoutError, MessageIdInvalid, DataInvalid):
            pass
        self._pending_click_captcha_message = None
        self.log.info(f'本地 OCR 点击了验证码按钮 "{button}".')
        return True

    async def on_captcha(self, message: Message, captcha: str):
        """
        可修改的回调函数.
            message: 包含验证码图片的消息
            captcha: OCR 识别的验证码
        """
        if await self.click_captcha_button(message, captcha):
            return
        if self._pending_click_captcha_message:
            await self.retry()
            return
        await message.reply(captcha)

    async def on_text(self, message: Message, text: str):
        """接收非验证码消息时, 检测关键词并确认签到成功或失败, 发送用户提示."""
        if not text:
            return
        if any(s in text for s in to_iterable(self.bot_text_ignore)):
            pass
        elif any(
            s in text for s in to_iterable(self.bot_account_fail_keywords) or default_keywords["account_fail"]
        ):
            self.log.warning(f"签到失败: 账户错误.")
            await self.fail()
        elif any(
            s in text
            for s in to_iterable(self.bot_too_many_tries_fail_keywords)
            or default_keywords["too_many_tries_fail"]
        ):
            self.log.warning(f"签到失败: 尝试次数过多.")
            await self.fail()
        elif any(s in text for s in to_iterable(self.bot_checked_keywords) or default_keywords["checked"]):
            self.log.info(f"今日已经签到过了.")
            await self.finish(RunStatus.NONEED, "今日已签到")
        elif any(s in text for s in to_iterable(self.bot_fail_keywords) or default_keywords["fail"]):
            self.log.info(f"签到失败: 验证码错误或网络错误, 正在重试.")
            await self.retry()
        elif any(s in text for s in to_iterable(self.bot_success_keywords) or default_keywords["success"]):
            if await self.before_success():
                if self.bot_success_pat:
                    matches = re.search(self.bot_success_pat, text)
                    if matches:
                        try:
                            self.log.info(
                                f"[yellow]签到成功[/]: + {matches.group(1)} 分 -> {matches.group(2)} 分."
                            )
                        except IndexError:
                            try:
                                self.log.info(f"[yellow]签到成功[/]: 当前/增加 {matches.group(1)} 分.")
                            except IndexError:
                                self.log.info(f"[yellow]签到成功[/].")
                    else:
                        matches = re.search(r"\d+", text)
                        if matches:
                            self.log.info(f"[yellow]签到成功[/]: 当前/增加 {matches.group(0)} 分.")
                        else:
                            self.log.info(f"[yellow]签到成功[/].")
                else:
                    self.log.info(f"[yellow]签到成功[/].")
                await self.after_success()
                await self.finish(RunStatus.SUCCESS, "签到成功")
        else:
            await self.on_unexpected_text(message)

    async def on_unexpected_text(self, message: Message):
        return await self.gpt_handle_message(message, unexpected=True)

    async def gpt_handle_message(self, message: Message, unexpected: bool = True):
        """本地启发式处理异常消息, 不再调用远端 GPT 服务."""
        content = message.text or message.caption
        if content:
            spec = content.replace("\n", " ")
            if unexpected:
                self.log.warning(f"接收到异常返回信息: {spec}, 正在尝试本地规则处理.")
            else:
                self.log.info(f"正在使用本地规则回答问题.")
            if (
                message.reply_markup
                and isinstance(message.reply_markup, InlineKeyboardMarkup)
                and message.reply_markup.inline_keyboard
            ):
                buttons = [b.text for r in message.reply_markup.inline_keyboard for b in r]
            else:
                buttons = []

            if buttons:
                button_specs = [f"'{b}'" for b in buttons]
                self.log.debug(f"当前按钮: {', '.join(button_specs)}")
                positive_keywords = (
                    "签到",
                    "簽到",
                    "打卡",
                    "开始",
                    "開始",
                    "验证",
                    "驗證",
                    "继续",
                    "繼續",
                    "确认",
                    "確認",
                    "确定",
                    "確定",
                    "提交",
                    "下一步",
                    "领取",
                    "領取",
                )
                negative_keywords = ("取消", "退出", "关闭", "關閉", "结束", "結束", "失败", "錯誤", "错误")
                for button in buttons:
                    if any(k in button for k in negative_keywords):
                        continue
                    if any(k in button for k in positive_keywords):
                        try:
                            await message.click(button)
                        except (TimeoutError, MessageIdInvalid):
                            pass
                        self.log.info(f'本地规则点击了按钮 "{button}".')
                        return True

            answer = self._solve_local_text_challenge(content)
            if answer:
                await message.reply(answer)
                self.log.info(f'本地规则回复了 "{answer}".')
                return True

            if unexpected:
                self.log.warning(f"本地规则无法处理该消息, 为了避免风险签到器将停止.")
                await self.fail()
                return False
            else:
                self.log.warning(f"本地规则无法处理该消息.")
                return False

    def _solve_local_text_challenge(self, content: str) -> Optional[str]:
        """处理简单文本算术题."""
        if not content:
            return None
        normalized = content.replace("＋", "+").replace("－", "-").replace("×", "*").replace("x", "*")
        normalized = normalized.replace("X", "*").replace("÷", "/").replace("／", "/")
        match = re.search(r"(-?\d+)\s*([+\-*/])\s*(-?\d+)", normalized)
        if not match:
            return None
        left, op, right = match.groups()
        left = int(left)
        right = int(right)
        if op == "+":
            return str(left + right)
        if op == "-":
            return str(left - right)
        if op == "*":
            return str(left * right)
        if op == "/" and right:
            result = left / right
            return str(int(result)) if result.is_integer() else str(result)
        return None

    async def retry(self):
        """执行重试, 重新发送签到指令."""
        self.current_retries += 1
        if self.current_retries <= self.valid_retries:
            await asyncio.sleep(self.bot_retry_wait)
            await self.send_checkin(retry=True)
        else:
            self.log.warning("超过最大重试次数.")
            await self.finish(RunStatus.FAIL, "超过最大重试次数")

    async def fail(self, status: RunStatus = RunStatus.FAIL, message: str = None):
        """设定签到器失败."""
        return await self.finish(status, message, fail=True)

    async def finish(self, status: RunStatus = RunStatus.SUCCESS, message: str = None, fail=False):
        """设定签到器结束."""

        if fail:
            self.current_retries = float("inf")
        self.ctx.status = status
        self.ctx.status_info = message
        self.finished.set()
        return self

    async def wait_until(self, pattern: str = ".", timeout: float = None):
        """等待特定消息出现."""
        self._waiting[pattern] = e = asyncio.Event()
        try:
            await asyncio.wait_for(e.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        else:
            msg: Message = self._waiting[pattern]
            return msg

    async def before_success(self):
        """签到成功前钩子, 返回值为 True 将继续执行并显示成功信息."""
        return True

    async def after_success(self):
        """签到成功后钩子."""
        pass


class AnswerBotCheckin(BotCheckin):
    """签到类, 用于按钮模式签到."""

    bot_answer_button_message_pat: str = None  # 回答按键消息内容的 regex 条件
    bot_answer_button_pat: str = None  # 所有按键需要满足的 regex 条件

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.mutex = asyncio.Lock()  # 实例变量异步锁
        self.operable = asyncio.Condition(self.mutex)  # 当 message 被设定时提醒等待的异步线程.
        self.message: Message = None  # 存储可用于回复的带按钮的消息.

    async def walk_history(self, limit=0):
        """处理 limit 条历史消息, 并检测是否有验证码或按钮."""
        try:
            answer = None
            captcha = None
            async for m in self.client.get_chat_history(self.chat_name or self.bot_username, limit=limit):
                if MessageType.ANSWER in self.message_type(m):
                    answer = answer or m
                if MessageType.CAPTCHA in self.message_type(m):
                    captcha = captcha or m
                if answer and captcha:
                    break
            else:
                return False
            await self.on_answer(answer)
            await self.on_photo(captcha)
            return True
        except Exception as e:
            self.log.warning("读取历史消息失败, 将不再读取历史消息.")
            show_exception(e)
            return False

    def get_keys(self, message: Message):
        """获得所有按钮信息."""
        reply_markup = message.reply_markup
        if isinstance(reply_markup, InlineKeyboardMarkup):
            return [k.text for r in reply_markup.inline_keyboard for k in r]
        elif isinstance(reply_markup, ReplyKeyboardMarkup):
            return [k.text for r in reply_markup.keyboard for k in r]

    def is_valid_answer(self, message: Message):
        """确认消息是回复按钮消息."""
        if not message.reply_markup:
            return False
        if self.bot_answer_button_message_pat:
            text = message.text or message.caption
            if not text:
                return False
            if not re.search(self.bot_answer_button_message_pat, text):
                return False
        if self.bot_answer_button_pat:
            for k in self.get_keys(message):
                if not re.search(self.bot_answer_button_pat, k):
                    return False
        return True

    def message_type(self, message: Message):
        """分析传入消息的类型为验证码或文字."""
        if self.is_valid_answer(message):
            return MessageType.ANSWER | super().message_type(message)
        else:
            return super().message_type(message)

    async def message_handler(self, client: Client, message: Message, type=None):
        """消息处理入口函数."""
        type = type or self.message_type(message)
        if MessageType.ANSWER in type:
            await self.on_answer(message)
        await super().message_handler(client, message, type=type)

    async def on_answer(self, message: Message):
        """当检测到带按钮的用于回答的消息时保存其信息."""
        async with self.mutex:
            if self.message:
                if self.message.date > message.date:
                    return
                else:
                    self.message = message
            else:
                self.message = message
                self.operable.notify()

    async def on_captcha(self, message: Message, captcha: str):
        """
        可修改的回调函数.
        参数:
            message: 包含验证码图片的消息
            captcha: OCR 识别的验证码
        """
        async with self.operable:
            if not self.message:
                await self.operable.wait()
            # Convert captcha to lowercase and compare with lowercase keys
            match = [(k, fuzz.ratio(k.lower(), captcha.lower())) for k in self.get_keys(self.message)]
            max_k, max_r = max(match, key=lambda x: x[1])
            if max_r < 75:
                self.log.info(f'未能找到对应 "{captcha}" 的按键, 正在重试.')
                await self.retry()
            else:
                try:
                    await self.message.click(max_k)
                except (TimeoutError, MessageIdInvalid, DataInvalid):
                    pass
