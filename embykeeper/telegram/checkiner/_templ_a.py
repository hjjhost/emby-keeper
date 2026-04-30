import asyncio
import random
from typing import List, Optional, Union

from loguru import logger
from pydantic import BaseModel, ValidationError
from pyrogram.types import Message
from pyrogram.errors import MessageIdInvalid
from pyrogram.raw.types.messages import BotCallbackAnswer

from embykeeper.utils import to_iterable

from . import BotCheckin

__ignore__ = True


class TemplateACheckinConfig(BaseModel):
    # fmt: off
    name: Optional[str] = None  # 签到器的名称
    use_button_answer: bool = None  # 点击按钮后等待并识别响应
    bot_text_ignore_answer: Union[str, List[str]] = None  # 忽略的响应文本
    bot_fail_keywords: Union[str, List[str]] = None  # 签到错误将重试时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_success_keywords: Union[str, List[str]] = None  # 成功时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_success_pat: Optional[str] = None  # 当接收到成功消息后, 从消息中提取数字的模式
    bot_captcha_len: Optional[int] = None  # 验证码长度的可能范围
    bot_text_ignore: Union[str, List[str]] = None  # 当含有列表中的关键词, 即忽略该消息, 置空不限制
    bot_checkin_caption_pat: Optional[str] = None  # 当 Bot 返回图片时, 仅当符合该 regex 才识别为验证码, 置空不限制
    bot_checkin_cmd: Optional[str] = None  # Bot 依次执行的签到命令
    bot_use_captcha: Optional[bool] = None  # 当 Bot 返回图片时, 识别验证码并调用 on_captcha
    bot_checkin_button: Union[str, List[str]] = None  # 签到按钮文本
    templ_panel_keywords: Union[str, List[str]] = None  # 面板关键词
    # fmt: on


class TemplateACheckin(BotCheckin):
    bot_text_ignore_answer = ["Done"]
    use_button_answer = True
    bot_checkin_cmd = "/start"
    bot_checkin_button = ["签到", "簽到"]
    templ_panel_keywords = None

    async def init(self):
        try:
            self.t_config = TemplateACheckinConfig.model_validate(self.config)
        except ValidationError as e:
            self.log.warning(f"初始化失败: 签到自定义模板 A 的配置错误:\n{e}")
            return False

        self.name = self.t_config.name or self.name
        self.use_button_answer = (
            self.t_config.use_button_answer
            if self.t_config.use_button_answer is not None
            else self.use_button_answer
        )
        self.bot_text_ignore_answer = self.t_config.bot_text_ignore_answer or self.bot_text_ignore_answer
        self.bot_fail_keywords = self.t_config.bot_fail_keywords or self.bot_fail_keywords
        self.bot_success_keywords = self.t_config.bot_success_keywords or self.bot_success_keywords
        self.bot_success_pat = self.t_config.bot_success_pat or self.bot_success_pat
        self.bot_captcha_len = self.t_config.bot_captcha_len or self.bot_captcha_len
        self.bot_text_ignore = self.t_config.bot_text_ignore or self.bot_text_ignore
        self.bot_checkin_caption_pat = self.t_config.bot_checkin_caption_pat or self.bot_checkin_caption_pat
        self.bot_checkin_cmd = self.t_config.bot_checkin_cmd or self.bot_checkin_cmd
        self.bot_checkin_button = self.t_config.bot_checkin_button or self.bot_checkin_button
        self.templ_panel_keywords = self.t_config.templ_panel_keywords or self.templ_panel_keywords
        self.bot_use_captcha = (
            self.t_config.bot_use_captcha
            if self.t_config.bot_use_captcha is not None
            else self.bot_use_captcha
        )

        self.log = logger.bind(scheme="telechecker", name=self.name, username=self.client.me.full_name)
        return True

    async def message_handler(self, client, message: Message):
        text = message.caption or message.text
        if self.is_click_captcha_prompt(message):
            self.remember_click_captcha_message(message)
            return
        if (
            text
            and message.reply_markup
            and (
                (
                    self.templ_panel_keywords
                    and any(keyword in text for keyword in to_iterable(self.templ_panel_keywords))
                )
                or (getattr(message, "is_first_response", False) and not message.edit_date)
            )
        ):
            keys = [k.text for r in message.reply_markup.inline_keyboard for k in r]
            for k in keys:
                if any(btn in k for btn in self.bot_checkin_button):
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    try:
                        answer: BotCallbackAnswer = await message.click(k)
                    except TimeoutError:
                        self.log.debug(f"点击签到按钮无响应, 可能按钮未正确处理点击回复. 一般来说不影响签到.")
                    except MessageIdInvalid:
                        pass
                    else:
                        await self.on_button_answer(answer)
                    return
            else:
                self.log.warning(f"签到失败: 账户错误.")
                return await self.fail()

        if message.text and "请先点击下面加入我们的" in message.text:
            self.log.warning(f"签到失败: 账户错误.")
            return await self.fail()

        await super().message_handler(client, message)

    async def on_button_answer(self, answer: BotCallbackAnswer):
        if self.use_button_answer:
            if not isinstance(answer, BotCallbackAnswer):
                self.log.warning(f"签到失败: 签到按钮指向 URL, 不受支持.")
                return await self.fail()
            if answer.message and not any(ignore in answer.message for ignore in self.bot_text_ignore_answer):
                await self.on_text(Message(id=0, text=answer.message), answer.message)


def use(**kw):
    return type("TemplatedClass", (TemplateACheckin,), kw)
