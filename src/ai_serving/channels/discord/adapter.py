"""Optional discord.py transport; importing the runtime needs no Discord SDK."""

from typing import AsyncIterator, Any

from ai_serving.contracts.messages import Message
from ai_serving.channels.streaming import publish_stream


class DiscordAdapter:
    def __init__(self, client: Any, *, edit_interval: float = 0.5):
        self.client = client
        self.edit_interval = edit_interval
        self._sent: dict[str, Any] = {}

    def normalize(self, event: Any) -> Message:
        # Threads are channels in Discord. parent_id identifies their parent.
        parent_id = getattr(event.channel, "parent_id", None)
        reference = getattr(event, "reference", None)
        replied = getattr(reference, "resolved", None)
        replies_to_bot = getattr(replied, "author", None) == self.client.user
        return Message(
            message_id=str(event.id), user_id=str(event.author.id),
            channel_id=str(parent_id or event.channel.id), text=event.content,
            thread_id=str(event.channel.id) if parent_id else None,
            attachments=tuple(item.url for item in event.attachments),
            mentions_bot=self.client.user in event.mentions or replies_to_bot,
            reply_to=str(reference.message_id) if reference and reference.message_id else None,
            is_bot=event.author.bot,
        )

    async def send(self, message: Message, text: str) -> str:
        channel_id = int(message.thread_id or message.channel_id)
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        import discord

        sent = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        self._sent[str(sent.id)] = sent
        return str(sent.id)

    async def edit(self, message_id: str, text: str) -> None:
        import discord

        await self._sent[message_id].edit(content=text, allowed_mentions=discord.AllowedMentions.none())

    async def publish(self, message: Message, chunks: AsyncIterator[str]) -> str:
        before = set(self._sent)
        try:
            return await publish_stream(self, message, chunks, edit_interval=self.edit_interval)
        finally:
            for message_id in set(self._sent) - before:
                self._sent.pop(message_id, None)


def create_client():
    """Install the optional discord extra before using the real transport."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    return discord.Client(intents=intents)
