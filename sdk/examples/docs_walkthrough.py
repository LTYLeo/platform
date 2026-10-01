"""Every example from docs.html, run against a live server.

This exists so the documentation cannot silently rot. The docs previously showed
``reply.output.content`` (``output`` is a dict by design; the shortcut is
``reply.content``) and ``async for event in await client.chat.stream(...)`` (the
async client returns an async generator, so there is nothing to await). Both
looked plausible and neither worked. Running the page's examples is the only
thing that catches that class of mistake.

Usage::

    # 1. start the server (a stub model backend is fine)
    TAI_MODEL_BACKEND=stub python3 -m uvicorn server.main:app --port 8150

    # 2. register a user and mint a key
    curl -s -c /tmp/jar -X POST http://127.0.0.1:8150/api/auth/register \
      -H 'Content-Type: application/json' \
      -d '{"name":"Docs","email":"docs@tai.dev","password":"supersecret1"}'
    curl -s -b /tmp/jar -X POST http://127.0.0.1:8150/api/keys \
      -H 'Content-Type: application/json' -d '{"name":"docs"}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])' > /tmp/dv.key

    # 3. run it
    TAI_API_KEY=$(cat /tmp/dv.key) TAI_BASE_URL=http://127.0.0.1:8150 \
      python3 sdk/examples/docs_walkthrough.py

Exits non-zero on the first broken example.
"""

import os, sys
os.environ.setdefault("TAI_API_KEY", open("/tmp/dv.key").read().strip())
os.environ.setdefault("TAI_BASE_URL", "http://127.0.0.1:8150")

from tai_sdk import TAI, InsufficientBalanceError, ModelNotAvailableError
from tai_sdk.types import MessageDelta, MessageDone

n = 0
def ok(m):
    global n; n += 1
    print(f"  \033[32m✓\033[0m {m}")

# --- Quickstart: client reads TAI_API_KEY and TAI_BASE_URL -----------------
client = TAI()
ok("TAI() 自动读取 TAI_API_KEY / TAI_BASE_URL")

reply = client.chat.create(
    model="tfmf",
    messages=[{"role": "user", "content": "Explain LoRA in two sentences."}],
)
print(f"      output: {reply.content[:56]}...")
print(f"      usage : in={reply.usage.input_tokens} out={reply.usage.output_tokens} ¥{reply.usage.cost_cny}")
ok("Quickstart 示例")

# --- Streaming -------------------------------------------------------------
stream = client.chat.stream(
    model="tfmf",
    messages=[{"role": "user", "content": "Count to five."}],
)
frags, done = 0, None
for event in stream:
    if isinstance(event, MessageDelta):
        frags += 1
    elif isinstance(event, MessageDone):
        done = event
ok(f"Streaming 示例（{frags} 个 delta，done 带 usage={done.usage.output_tokens}）")

# --- Assistants ------------------------------------------------------------
assistant = client.assistants.create(
    model="tfmf",
    name="Tutor",
    instructions="You are a patient tutor. Answer briefly.",
    metadata={"team": "education"},
)
print(f"      assistant.id = {assistant.id}")
ok("Assistants.create 示例")

got = client.assistants.get(assistant.id);            ok("Assistants.get")
client.assistants.update(assistant.id, instructions="Be even briefer."); ok("Assistants.update")
page = client.assistants.list(limit=2);               ok(f"Assistants.list（{len(page.data)} 条, has_more={page.has_more}）")

# --- Threads ---------------------------------------------------------------
thread = client.threads.create(assistant_id=assistant.id, title="LoRA questions")
print(f"      thread.id = {thread.id}")
ok("Threads.create 示例")

client.threads.get(thread.id);                        ok("Threads.get")
client.threads.update(thread.id, title="Renamed");    ok("Threads.update")
client.threads.list(limit=5);                         ok("Threads.list")

# --- Messages --------------------------------------------------------------
exchange = client.threads.messages.create(
    thread.id,
    content="What does rank mean in LoRA?",
    temperature=0.3,
    max_output_tokens=256,
)
print(f"      user     : {exchange.user_message.content}")
print(f"      assistant: {exchange.assistant_message.content[:50]}...")
print(f"      cost     : ¥{exchange.usage.cost_cny}")
ok("Messages.create 示例")

for _ in client.threads.messages.create(thread.id, content="Go on", stream=True):
    pass
ok("Messages.create(stream=True) 示例")

mp = client.threads.messages.list(thread.id, limit=20, order="asc")
for m in mp.data:
    _ = m.role, m.content
ok(f"Messages.list 示例（order=asc，{len(mp.data)} 条）")

# --- Pagination ------------------------------------------------------------
p = client.assistants.list(limit=2)
loops = 0
while p.has_more and loops < 5:
    p = client.assistants.list(limit=2, after=p.last_id)
    loops += 1
ok(f"Pagination 示例（翻页 {loops} 次，has_more 正常）")

# --- Models ----------------------------------------------------------------
models = client.models.list()
for m in models:
    _ = m.id, m.input_cny_per_1m, m.output_cny_per_1m
ok(f"Models.list 示例（{len(models)} 个模型）")
tfmf = client.models.retrieve("tfmf")
ok(f"Models.retrieve('tfmf') -> {tfmf.name}")

# --- Usage -----------------------------------------------------------------
u = client.usage.retrieve()
print(f"      requests={u.requests_this_month} tokens={u.tokens_this_month} ¥{u.cost_cny_this_month} balance=¥{u.balance_cny}")
print(f"      free_tokens={u.free_tokens}")
ok("Usage.retrieve 示例")

# --- Errors ----------------------------------------------------------------
try:
    client.chat.create(model="giggle", messages=[{"role": "user", "content": "hi"}])
    print("  \033[31m✗ ModelNotAvailableError 未抛出\033[0m"); sys.exit(1)
except ModelNotAvailableError as exc:
    ok(f"Errors 示例：ModelNotAvailableError code={exc.code}")

try:
    client.models.retrieve("gpt-9")
    print("  \033[31m✗ ModelNotFoundError 未抛出\033[0m"); sys.exit(1)
except Exception as exc:
    ok(f"未知模型 -> {type(exc).__name__}")

# --- Async -----------------------------------------------------------------
import asyncio
from tai_sdk import AsyncTAI

async def main():
    async with AsyncTAI() as ac:
        r = await ac.chat.create(model="tfmf", messages=[{"role": "user", "content": "Hello!"}])
        assert r.content
        # Docs say: no `await` before .stream(); consume with `async for`.
        stream = ac.chat.stream(
            model="tfmf",
            messages=[{"role": "user", "content": "Count to three."}],
        )
        seen = 0
        async for _ev in stream:
            seen += 1
        assert seen > 0
        # And the same for a thread message
        a = await ac.assistants.create(model="tfmf", name="AT")
        th = await ac.threads.create(assistant_id=a.id)
        seen2 = 0
        async for _ev in ac.threads.messages.create(th.id, content="go", stream=True):
            seen2 += 1
        assert seen2 > 0
        await ac.threads.delete(th.id)
        await ac.assistants.delete(a.id)
        return seen, seen2

s1, s2 = asyncio.run(main())
ok(f"Async 示例（chat.stream {s1} 事件，messages.create(stream) {s2} 事件）")

# --- cleanup ---------------------------------------------------------------
client.assistants.delete(assistant.id)
ok("Assistants.delete")
after = client.threads.get(thread.id)
assert after.assistant_id is None, after.assistant_id
ok("删除助手后会话保留，assistant_id 置空（验证完再删会话）")
client.threads.delete(thread.id)
ok("Threads.delete")

print(f"\n\033[32m文档中 {n} 个示例全部跑通\033[0m")
