import asyncio
import random
import emoji
from thefuzz import process
from pyrogram.types import Message
from pyrogram.errors import RPCError

from . import AnswerBotCheckin


class TerminusCheckin(AnswerBotCheckin):
    name = "终点站"
    bot_username = "EmbyPublicBot"
    bot_checkin_cmd = ["/cancel", "/checkin"]
    bot_text_ignore = ["会话已取消", "没有活跃的会话"]
    bot_checked_keywords = ["今天已签到"]
    additional_auth = ["visual"]
    max_retries = 1
    bot_use_history = 3

    async def on_photo(self, message: Message):
        """使用本地 OCR 分析验证码图片并点击匹配选项."""
        if message.reply_markup:
            clean = lambda o: emoji.replace_emoji(o, "").replace(" ", "")
            keys = [k for r in message.reply_markup.inline_keyboard for k in r]
            options = [k.text for k in keys]
            options_cleaned = [clean(o) for o in options]
            if len(options) < 2:
                return
            result = clean(await self.recognize_captcha_text(message))
            if not result:
                self.log.warning(f"签到失败: 验证码识别错误.")
                return await self.fail()
            matched, score = process.extractOne(result, options_cleaned)
            if score < 50:
                self.log.warning(f"本地 OCR 答案难以与可用选项相匹配 (分数: {score}/100).")
            result = options[options_cleaned.index(matched)]
            await asyncio.sleep(random.uniform(0.5, 1.5))
            try:
                await message.click(result)
            except RPCError:
                self.log.warning("按钮点击失败.")
