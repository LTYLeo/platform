"""Async client, including concurrent requests and async streaming.

    export TAI_API_KEY="sk-tai-..."
    python examples/async_chat.py
"""

from __future__ import annotations

import asyncio
import sys

from tai_sdk import AsyncTAI
from tai_sdk.types import MessageDelta, MessageDone, MessageStart


async def stream_one(client: AsyncTAI, model: str, prompt: str) -> str:
    print("> %s" % prompt)
    parts = []
    async for event in client.chat.stream(
        model=model, messages=[{"role": "user", "content": prompt}]
    ):
        if isinstance(event, MessageStart):
            print("[stream %s]" % event.id)
        elif isinstance(event, MessageDelta):
            parts.append(event.delta)
            sys.stdout.write(event.delta)
            sys.stdout.flush()
        elif isinstance(event, MessageDone):
            if event.usage:
                print(
                    "\n[done: %s in / %s out]"
                    % (event.usage.input_tokens, event.usage.output_tokens)
                )
    print()
    return "".join(parts)


async def main() -> None:
    async with AsyncTAI() as client:
        models = await client.models.list()
        if not models.data:
            raise SystemExit("No live models are available.")
        model = models.data[0].id

        # Several independent completions at once.
        prompts = ["Name a fruit.", "Name a colour.", "Name a city."]
        replies = await asyncio.gather(
            *(
                client.chat.create(
                    model=model, messages=[{"role": "user", "content": prompt}]
                )
                for prompt in prompts
            )
        )
        for prompt, reply in zip(prompts, replies):
            print("%-16s -> %s" % (prompt, reply.content))

        # A full assistant + thread exchange, awaited one step at a time.
        assistant = await client.assistants.create(
            model=model,
            name="Async Buddy",
            instructions="Answer in one short sentence.",
        )
        thread = await client.threads.create(assistant_id=assistant.id, title="Async demo")
        try:
            exchange = await client.threads.messages.create(thread.id, content="What is 7*6?")
            print("reply:", exchange.content)
        finally:
            await client.threads.delete(thread.id)
            await client.assistants.delete(assistant.id)

        await stream_one(client, model, "Say goodbye in three words.")


if __name__ == "__main__":
    asyncio.run(main())
