"""Incremental SSE streaming with the sync client.

    export TAI_API_KEY="sk-tai-..."
    python examples/streaming.py

``client.chat.stream(...)`` opens the request immediately (so auth/validation
errors raise here) and yields typed events:

* ``MessageStart`` — id, model, thread_id (``None`` for stateless chat)
* ``MessageDelta`` — one text fragment; print it as it arrives
* ``MessageDone``  — the full content plus token usage

An ``error`` frame raises the matching ``tai_sdk.errors.APIError`` subclass.
The HTTP response is closed for you even if you ``break`` out of the loop.
"""

from __future__ import annotations

import sys

from tai_sdk import TAI
from tai_sdk.types import MessageDelta, MessageDone, MessageStart


def stream_once(client: TAI, model: str, prompt: str) -> str:
    print("> %s" % prompt)
    text = []
    stream = client.chat.stream(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_output_tokens=256,
    )
    with stream:  # also closes the response if we break early
        for event in stream:
            if isinstance(event, MessageStart):
                print("[stream %s started on %s]" % (event.id, event.model))
            elif isinstance(event, MessageDelta):
                text.append(event.delta)
                sys.stdout.write(event.delta)
                sys.stdout.flush()
            elif isinstance(event, MessageDone):
                if event.usage:
                    print(
                        "\n[done: %s in / %s out, %s CNY]"
                        % (
                            event.usage.input_tokens,
                            event.usage.output_tokens,
                            event.usage.cost_cny,
                        )
                    )
    print()
    return "".join(text)


def main() -> None:
    with TAI() as client:
        models = client.models.list()
        if not models.data:
            raise SystemExit("No live models are available.")
        model = models.data[0].id

        stream_once(client, model, "Count from one to five, one word per line.")

        # Early exit still closes the HTTP response.
        stream = client.chat.stream(
            model=model,
            messages=[{"role": "user", "content": "Write a very long essay."}],
        )
        for event in stream:
            if isinstance(event, MessageDelta):
                print("stopped early after %r" % event.delta)
                break
        stream.close()

        # Thread streams persist the assistant message and record usage once.
        thread = client.threads.create(model=model, title="Streaming demo")
        try:
            events = client.threads.messages.create(thread.id, content="Hi!", stream=True)
            thread_id = None
            streamed = []
            for event in events:
                if isinstance(event, MessageStart):
                    thread_id = event.thread_id
                elif isinstance(event, MessageDelta):
                    streamed.append(event.delta)
                elif isinstance(event, MessageDone):
                    print("persisted as %s in thread %s" % (event.id, thread_id))
            print("streamed:", "".join(streamed))
        finally:
            client.threads.delete(thread.id)


if __name__ == "__main__":
    main()
