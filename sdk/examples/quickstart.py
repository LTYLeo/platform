"""The smallest useful TAI SDK program.

    export TAI_API_KEY="sk-tai-..."      # from the developer dashboard
    python examples/quickstart.py

The base URL is resolved automatically (see the README): ``base_url=`` argument,
``TAI_BASE_URL``, ``~/.tai/config.json``, the discovery document, then
``https://api.tai-research.dev``.
"""

from __future__ import annotations

from tai_sdk import TAI


def main() -> None:
    with TAI() as client:
        print("endpoint: %s (from %s)" % (client.base_url, client.base_url_source))

        models = client.models.list()
        for model in models:
            print(
                "- %-12s %-10s context=%-6s in=%.2f/1M out=%.2f/1M"
                % (
                    model.id,
                    model.name,
                    model.context_window,
                    model.input_cny_per_1m or 0.0,
                    model.output_cny_per_1m or 0.0,
                )
            )
        if not models.data:
            raise SystemExit("No live models are available.")
        model_id = models.data[0].id

        # Stateless chat: nothing is stored server-side.
        completion = client.chat.create(
            model=model_id,
            messages=[
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "Explain tail recursion in one sentence."},
            ],
            temperature=0.3,
            max_output_tokens=128,
        )
        print("\nreply:", completion.content)
        if completion.usage:
            print(
                "usage: %s in / %s out, cost=%s CNY, estimated=%s"
                % (
                    completion.usage.input_tokens,
                    completion.usage.output_tokens,
                    completion.usage.cost_cny,
                    completion.usage.estimated,
                )
            )

        # Reusable assistant + persistent thread.
        assistant = client.assistants.create(
            model=model_id,
            name="Study Buddy",
            instructions="You are a patient tutor. Keep answers short.",
        )
        print("\ncreated assistant:", assistant.id)

        thread = client.threads.create(assistant_id=assistant.id, title="Quickstart")
        exchange = client.threads.messages.create(thread.id, content="What is a factorial?")
        print("assistant said:", exchange.content)

        messages = client.threads.messages.list(thread.id)
        print("thread now has %d message(s), newest first:" % len(messages))
        for message in messages:
            print("  %-9s %s" % (message.role, (message.content or "")[:60]))

        # Usage is recorded exactly once per generation, so the dashboard matches.
        usage = client.usage.retrieve()
        print("\nusage summary:", usage.raw)

        # Clean up so repeated runs stay tidy.
        client.threads.delete(thread.id)
        client.assistants.delete(assistant.id)


if __name__ == "__main__":
    main()
