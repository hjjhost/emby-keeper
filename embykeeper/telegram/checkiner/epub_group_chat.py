import asyncio

from embykeeper.utils import to_iterable

from . import BotCheckin

__ignore__ = True


class EPubGroupChatCheckin(BotCheckin):
    name = "EPub 电子书库群组每日发言"
    chat_name = "libhsulife"
    additional_auth = ["prime"]
    bot_use_captcha = False

    async def send_checkin(self, retry=False):
        times = self.config.get("times", 5)
        min_letters = self.config.get("letters", 7)
        candidates = self.config.get(
            "messages",
            [
                "春风入卷书声远",
                "夜雨敲窗灯影深",
                "一页清欢留指畔",
                "半盏新茶待月明",
                "闲来展卷见山河",
                "纸上星河映旧梦",
                "微光照字意悠长",
            ],
        )
        lines = [line for line in candidates if len(line) >= min_letters]
        if len(lines) < times:
            return await self.fail(message="本地发言素材不足")
        lines = lines[:times]
        for l in lines:
            self.log.info(f"即将向群组发送水群消息: {l}.")
        await asyncio.sleep(10)
        cmds = to_iterable(lines)
        for i, cmd in enumerate(cmds):
            if retry and not i:
                await asyncio.sleep(self.bot_retry_wait)
            if i < len(cmds):
                await asyncio.sleep(self.bot_send_interval)
            await self.send(cmd)
        await self.finish(message="已发送发言")
        return

    async def message_handler(*args, **kw):
        return
