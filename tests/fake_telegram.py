from dataclasses import replace

from garde.delivery.sender import DeliverySender, SenderConfig


class FakeMessenger:
    """Records messages. script[chat_id] is a list of errors to raise on that chat's next sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.attempts: list[int] = []
        self.script: dict[int, list[Exception]] = {}

    async def send(self, chat_id: int, text: str) -> None:
        self.attempts.append(chat_id)
        queued = self.script.get(chat_id)
        if queued:
            raise queued.pop(0)
        self.sent.append((chat_id, text))


async def no_sleep(delay: float) -> None:
    return None


def make_sender(factory, messenger, **overrides) -> DeliverySender:
    config = replace(SenderConfig(pause_between_sends=0.0), **overrides)
    return DeliverySender(factory, messenger, config, sleep=no_sleep)