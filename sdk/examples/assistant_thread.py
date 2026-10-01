"""Assistants + threads + messages, with pagination and error handling.

    export TAI_API_KEY="sk-tai-..."
    python examples/assistant_thread.py
"""

from __future__ import annotations

from tai_sdk import TAI
from tai_sdk import errors


def page_through_assistants(client: TAI) -> None:
    """``after`` is the id of the last item you saw; ``data`` is newest-first."""
    seen = 0
    after = None
    while True:
        page = client.assistants.list(limit=5, after=after)
        for assistant in page.data:
            seen += 1
            print("  %s  %-20s model=%s" % (assistant.id, assistant.name, assistant.model))
        if not page.has_more or not page.last_id:
            break
        after = page.last_id
    print("  (%d assistant(s) total)" % seen)


def main() -> None:
    with TAI() as client:
        models = client.models.list()
        if not models.data:
            raise SystemExit("No live models are available.")
        model = models.data[0].id

        print("existing assistants:")
        page_through_assistants(client)

        assistant = client.assistants.create(
            model=model,
            name="Algorithms Tutor",
            instructions=(
                "You are a patient algorithms tutor. Explain the idea first, "
                "then give one short code sketch."
            ),
            metadata={"course": "algorithms", "level": "intro"},
        )
        print("\ncreated %s (%s)" % (assistant.id, assistant.name))

        thread = client.threads.create(
            assistant_id=assistant.id,
            title="Reversing a list",
            metadata={"topic": "lists"},
        )
        print("created thread %s (assistant=%s)" % (thread.id, thread.assistant_id))

        try:
            for question in (
                "How do I reverse a list in Python?",
                "What does that cost in memory?",
            ):
                exchange = client.threads.messages.create(
                    thread.id,
                    content=question,
                    temperature=0.4,
                    max_output_tokens=256,
                )
                print("\nQ: %s" % question)
                print("A: %s" % exchange.content)
                if exchange.usage:
                    print(
                        "   [%s in / %s out, %s CNY, estimated=%s]"
                        % (
                            exchange.usage.input_tokens,
                            exchange.usage.output_tokens,
                            exchange.usage.cost_cny,
                            exchange.usage.estimated,
                        )
                    )

            newest_first = client.threads.messages.list(thread.id, limit=10)
            print("\nnewest-first (%d):" % len(newest_first))
            for message in newest_first:
                print("  %-9s %s" % (message.role, (message.content or "")[:70]))

            oldest_first = client.threads.messages.list(thread.id, order="asc")
            print("oldest-first: %s" % ", ".join(m.role for m in oldest_first))

            # PATCH only the fields you pass; re-reading shows the result.
            renamed = client.threads.update(thread.id, title="Reversing a list (v2)")
            print("\nrenamed thread to %r (updated_at=%s)" % (renamed.title, renamed.updated_at))

            refreshed = client.assistants.update(
                assistant.id, instructions="Answer in at most two sentences."
            )
            print("assistant instructions are now %r" % refreshed.instructions)

        except errors.InvalidRequestError as exc:
            print("validation failed on %r: %s" % (exc.param, exc.message))
        except errors.APIError as exc:
            print("API error %s (%s): %s" % (exc.status_code, exc.code, exc.message))
        except errors.TAIError as exc:
            # Connectivity problems mention the base URL the SDK actually tried.
            print("could not reach the API: %s" % exc)
        finally:
            client.threads.delete(thread.id)  # messages cascade
            client.assistants.delete(assistant.id)
            print("\ncleaned up.")


if __name__ == "__main__":
    main()
