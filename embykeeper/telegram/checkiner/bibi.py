from ._templ_a import TemplateACheckin

from pyrogram.types import Message

__ignore__ = True


class BibiCheckin(TemplateACheckin):
    name = "比比"
    bot_username = "BBFreeFilm_bot"
    bot_use_captcha = False

    async def message_handler(self, client, message: Message):
        text = message.text or message.caption
        if text and "签到验证" in text:
            if "开始" in text:
                return
            if self.is_click_captcha_prompt(message):
                self.remember_click_captcha_message(message)
                return
            if not await self.gpt_handle_message(message, unexpected=False):
                self.log.info(f"签到失败: 智能解析错误, 正在重试.")
                return await self.retry()
            else:
                return
        await super().message_handler(client, message)
