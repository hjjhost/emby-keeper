import asyncio

from ..lock import pornfans_alert, pornfans_messager_mids_lock, pornfans_messager_mids
from . import BotCheckin

__ignore__ = True


class PornfansGameGroupCheckin(BotCheckin):
    name = "PornFans 游戏群发言"
    chat_name = "embytestflight"
    additional_auth = ["pornemby_pack"]
    bot_use_captcha = False

    async def send_checkin(self, retry=False):
        if pornfans_alert.get(self.client.me.id, False):
            self.log.warning("签到失败: 由于风险急停不进行发言")
            return await self.fail(message="由于风险急停不进行发言")

        min_letters = self.config.get("min_letters", self.config.get("letters", 8))
        max_letters = self.config.get("max_letters", 15)
        candidates = self.config.get(
            "messages",
            [
                "今天来冒个泡拿资格",
                "路过打个卡换资格",
                "大家今天都还好吗",
                "先发一句占个位吧",
            ],
        )
        valid_messages = [m for m in candidates if min_letters <= len(m) <= max_letters]
        if not valid_messages:
            return await self.fail(message="本地发言素材不足")

        message = valid_messages[0]
        self.log.info(f"即将向群组发送水群消息: {message}.")

        await asyncio.sleep(10)
        if retry:
            await asyncio.sleep(self.bot_retry_wait)

        message = await self.send(message)
        if not message:
            await self.fail(message="发送失败")
        else:
            async with pornfans_messager_mids_lock:
                if self.client.me.id not in pornfans_messager_mids:
                    pornfans_messager_mids[self.client.me.id] = []
            pornfans_messager_mids[self.client.me.id].append(message.id)
            await self.finish(message="已发送发言")
        return

    async def message_handler(*args, **kw):
        return
