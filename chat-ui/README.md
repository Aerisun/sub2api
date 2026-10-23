# User chat

The authenticated Vue `/chat` page hosts this React module in a same-origin iframe.
It uses vendored Vercel AI Elements Conversation, Message, Reasoning, PromptInput
and Suggestion components, installed from `https://registry.ai-sdk.dev`. Supporting
controls come from shadcn/ui. The Vercel license is preserved in
`AI-ELEMENTS-LICENSE`. One compatibility adjustment to the vendored AI Elements
source is in ReasoningContent: DOM properties are forwarded to its outer collapsible,
not to Streamdown, whose animation and direction properties have different types.

Build with `pnpm install --frozen-lockfile && pnpm run build` (pnpm 12.6.0).
The independent Tailwind 4/React 19 bundle is written to
`../frontend/public/chat-ui`; then build the normal Vue frontend. Separate stylesheets
keep AI Elements' Tailwind version from changing the existing Vue interface.
Run stream regressions with `node --experimental-strip-types --test src/stream.test.mjs`.

The browser submits its existing login bearer token to `/api/v1/chat/models` and
`/api/v1/user/chat` for history, images and background generation. It never submits or receives an API key. The server calls `/api/v1/chat/completions` internally; chat requests
reuse the existing authenticated gateway, model allowlist, routing, concurrency,
balance billing and usage logging. Models come from the live catalog; embedding,
reranking and media-generation entries are excluded from the chat picker.

`ChatCredential` provisions one durable reserved-prefix key per user on first use.
A partial unique index handles concurrent creation across processes. It selects an
available balance-billed group, preferring the composite group. User key lists and
search omit this credential, direct user key read/update/delete reject it, custom
keys cannot use the reserved namespace, and DTO serialization redacts its secret,
including nested usage records. Public gateway routes reject internal credentials;
only the JWT-authenticated chat route may use them. Key status and current user/group
permissions remain enforced by the existing gateway. The usage name is `在线对话`.

Conversation history is authoritative in PostgreSQL and partitioned by authenticated account.
The browser renders an IndexedDB cache first, checks a small metadata manifest with ETags,
and loads transcript bodies and image bytes only when needed. Existing browser-only
history is migrated on first visit; cache entries and asset identifiers are account-scoped.
Optimistic versions prevent stale tabs from overwriting newer server history.

A durable generation request ID starts one server-owned worker through the normal
billed gateway. Browser refresh/disconnection does not cancel that worker. Reopened
pages restore a saved checkpoint and attach one resumable SSE connection carrying only new text (Unicode character offsets),
then load the final canonical transcript. A duplicate start ID returns the existing
job without a second model invocation. Explicit Stop cancels the server worker.
At most three jobs per account can run concurrently. Job heartbeats detect interrupted
server processes; an interrupted process produces a recoverable error, never a silent
new billable request. Reload resilience does not imply worker survival across process restarts.

## Rollback

Back up the deployed binary, frontend entry and chat assets before replacement.
Before rolling back to a backend without chat-key protection, revoke all internal
keys: soft-delete them and replace their secret with an unusable tombstone. This is
necessary because the old backend does not redact the reserved namespace. Keep
usage records. The additive partial index can remain. Never restore the old backend
while active internal keys still exist.

## Model controls and reasoning effort

`gemini-web-flash-lite`, `gemini-web-flash`, `gemini-web-pro` and
`gemini-web-argon` are discovered through the live authenticated chat catalog
after group publication. They appear under Gemini with Standard Thinking
(`reasoning_effort: low`) and Extended Thinking (`reasoning_effort: high`).
Do not attach `thinking.type: enabled` to the standard mode: the native
GeminiClient bridge interprets that flag as extended thinking. There is no
off switch because suppressing a thought delta cannot prove the upstream has
stopped reasoning. Text-only capability is retained until image input is
verified. The ordinary authenticated chat, server tools, usage and durable
history use the same dedicated Gemini web route as API calls.

The composer uses AI Elements' PromptInputSelect controls with grouped model
options, provider marks, and a separate reasoning selector. Choices are remembered
per account and per model. Unknown models do not receive guessed reasoning controls.
Requests carry the explicitly displayed supported tier; the selector has no Default option.
Always-thinking models have no Off option. Sent messages retain the selected tier.

The currently deployed families use these native tiers (without a Default option): Qwen 3.8
Flash/Max: Off, low, medium, xhigh; GLM 5.2: Off, high, max; GLM 5.3 and Kimi K3:
low, high, max; DeepSeek V4.1 Flash: Off, low, high, max. The payload helper uses
Qwen's enable_thinking, GLM/DeepSeek's thinking.type and the provider's
reasoning_effort field; it never sends incompatible combinations or stale tiers
from another model. These controls follow the current gateway mappings and:

- Qwen: https://docs.qwencloud.com/api-reference/chat/openai-chat
- GLM 5.2: https://huggingface.co/zai-org/GLM-5.2/blob/main/README.md
- GLM 5.3: https://huggingface.co/zai-org/GLM-5.3/blob/main/README.md
- Kimi: https://github.com/MoonshotAI/Kimi-K3/blob/main/README.md
- DeepSeek: https://api-docs.deepseek.com/guides/thinking_mode/

Run `node --experimental-strip-types --test src/*.test.mjs` for native-tier,
request-parameter, stale-selection and SSE regressions. Browser tests additionally
capture the actual submitted request for all six currently available models.

The current catalog can also include GLM 5.3 Flash, NVIDIA Nemotron and Step 5.
GLM 5.3 Flash shares GLM 5.3's native tiers. Nemotron uses the deployed gateway's
verified budget mapping for low/medium/high/xhigh/max and supports Off. Step 5's
NVIDIA endpoint documents always-on reasoning without an adjustable effort field;
the UI therefore omits the effort selector instead of claiming unsupported depth controls. References:
https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b/build and
https://docs.api.nvidia.com/nim/reference/stepfun-ai-step-5-preview-infer.

PromptInput also has an optional `resetOnSubmit` setting (default true). This
controlled composer disables native form resets and clears text in its submit
handler, so Radix model and reasoning controls retain their current selections.

The selector now contains only concrete tiers. Previously saved `default` values
are resolved to explicit request settings: Qwen xhigh, GLM 5.2 max, and the current
gateway's low tier for GLM 5.3, Kimi, DeepSeek and Nemotron. The resolved value is
both displayed and submitted; existing concrete user choices remain unchanged.

Image messages support PNG/JPEG/WebP (up to 4 images, 5 MiB each), file selection,
clipboard paste and drag/drop. Vercel AI Elements owns attachment handling; requests
use OpenAI `image_url` content parts through the existing authenticated billing route.
Vision availability follows `frontend/src/config/aerisunModels.json`. Text-only models
reject conversations containing images without discarding their context. Image previews
and retries retain the original image. Browser history migrates from localStorage to
account-partitioned IndexedDB after a successful commit, then syncs to the account’s server history.


## Storage organization and maintenance

Migration `244_chat_history.sql` creates four additive PostgreSQL tables:

| Table | Purpose | Isolation and indexing |
| --- | --- | --- |
| `chat_conversations` | Titles, selected model, final transcripts, optimistic versions and delete tombstones | Composite `(user_id,id)` key; active user/update index |
| `chat_images` | One validated binary image per account and SHA-256 digest; content type and generated byte size | Composite account/asset key; unique `(user_id,digest)`; creation index |
| `chat_conversation_images` | Deduplicated conversation-to-asset references | Composite foreign keys enforce the same account for both endpoints; asset lookup index |
| `chat_generations` | Durable request IDs, state, sequence and temporary generation checkpoints | One active job per conversation; conversation/time index |

Transcript snapshots and asset references commit in one transaction. Transcripts contain
asset IDs rather than base64 duplicates. Completed generation checkpoint bodies are
cleared once their final transcript commits. Per-account reads, edits, image retrieval,
job progress and Stop operations all use the JWT subject; caller-supplied user IDs are ignored.
No API credentials or JWTs are stored in these tables. Uploaded images are checked against
actual file signatures, supported media types and the 5 MiB limit.

Deleting a conversation clears its transcript and asset references and retains a small
tombstone to reject delayed saves. Unreferenced assets have a 24-hour grace period.
An indexed cleanup runs ten minutes after startup and every six hours, reclaiming up to
1,000 unused images per pass; completed/stopped job metadata expires after 90 days.
Referenced images and active jobs are excluded. The standard PostgreSQL backup includes
all transcripts, images and checkpoints; there is no separate publicly served upload folder.
For storage inspection, group `SUM(size_bytes)` and counts from `chat_images` by `user_id`,
and inspect `chat_conversation_images` to find the conversations referencing an asset.

The development proxy accepts `CHAT_API_TARGET` (default `http://127.0.0.1:8080`).


Network and cache bounds: existing conversations send only the new user/assistant
pair; the server reconstructs context from its stored transcript. Start responses
omit transcript bodies. Progress uses one authenticated SSE connection with byte
framing and Unicode offsets; reconnects use exponential backoff and never start
another model request. Idle list checks use ETags every two minutes and on focus.
SHA-256 browser mappings avoid re-uploading the same image for twelve hours; server
deduplication remains authoritative. `245_chat_image_last_used.sql` keeps original
creation timestamps while tracking the upload/reference grace period separately.
Browser image cache is bounded to 64 MiB/200 entries per account. Browser transcript
cache stores individual conversations and rewrites only changed records, keeping
full bodies for twenty recent conversations, the selected conversation and unsynced
changes; other conversations retain lightweight metadata. Hashed chat bundles are
compressed and immutable; the entry HTML revalidates to avoid stale bundle hashes.


Foreground sends preempt background history synchronization for the same conversation.
They never await a history PUT or previous image migration. Only unsaved images
needed by the outgoing turn are uploaded before submitting; text-only turns call
fetch directly. New clients send a message-count/tail-ID anchor so the server can
accept a concurrently completed metadata save while rejecting another browser's
new messages or a deleted conversation. The server retains its short atomic job
acceptance transaction to preserve reload recovery and billing idempotency.


2026-10-20 server-tool updates: `Nemotron-3.5` capability lookup is case-insensitive.
Only current-turn images are forwarded; previous previews remain in history and
completed vision tool output is reused as text context. Recognition tools stream
inside the controlled thinking panel, fold when complete, and fold together when
reasoning ends. Generation shows actual task transitions and real result links.
Only text models configured for an auxiliary vision account invoke recognition;
native image models are not intercepted. Cloud history persists separate tool
snapshots while an authenticated in-memory broadcast avoids waiting for storage
checkpoints. The vendored ReasoningContent accepts React nodes so tool cards can
share its existing collapse behavior; string children still use MarkdownBody.

Recognition and generation share compact rows within the thinking panel. Real
generation uses AI Elements Shimmer with an indeterminate preview, including image
download time, and honors reduced motion. Generated previews use a 420 px desktop /
340 px mobile maximum width with rounded corners. Accompanying text is ordinary
Markdown body text. Internal historical-image policy is not inserted into user
messages; existing descriptions remain available for follow-up questions.

Empty Markdown image references are removed before the existing sanitizer/hardener
so they cannot appear as an English `[Image blocked: …]` placeholder after a real
image. This does not allow any additional image URL protocols. Ordinary adjacent
text, code examples and valid generated images remain intact.

Gemini reasoning uses the streaming Markdown renderer, stays open while the
reply is running, and separates a complete bold section title from the preceding
sentence. Formatting changes affect display only; raw saved reasoning, cache
hashes and tool offsets retain the original upstream content. Final SSE deltas
are applied before fetching the completed conversation.

The math plugin enables single-dollar inline expressions as well as display math.
Math rendering leaves fenced code and unpaired currency dollar signs literal.


## Codex, Fast and prompt cache (2026-10-20)

All seven published GPT-6/GPT-5.6 text models have explicit native reasoning tiers. Astra and 6.1 Sol cannot switch reasoning off; low/medium/high/xhigh/max remain distinct. The composer uses a separate lightning button for Fast. New drafts default to off. Enabling it shows a 3.5-second “Fast 已开启，计费 ×4” status toast and sends `params.service_tier: "fast"`; standard drafts omit that field. The stored assistant message retains `serviceTier: "fast"`. Changing models clears Fast. No credential is added to client-side requests.

The authenticated server's generation worker forwards the selected tier and reasoning effort and assigns `X-Session-Id: chat_<conversation UUID>`. This identity is stable across turns, model effort and Fast changes; downstream tenant isolation remains authoritative. Full stored message context and returned reasoning summaries are replayed. Summaries stream while the model is generating and survive server history reload. Displayed content is the provider's official summary, and some requests return no summary even with detailed mode enabled. Do not fabricate thought text or treat encrypted reasoning bytes as displayable content.

## Image generation model labels (2026-10-21)

Completed image tools display `metadata.model` below the answer, independently
of the thinking panel, and in the expanded tool summary. This model is recorded
by the accepted image task and persists through the existing tool history/SSE
metadata. Multiple model values are deduplicated per answer. Legacy completed
tools without the field show “未记录”; the chat model is never substituted.

`qa/image-tool-model.js` exercises all three native image model labels, collapsed
thinking, tool links, live completion, history reload and 390px light/dark layouts
on the public page with isolated fixture APIs and images. It submits no paid
image or text requests.

## Native Gemini and Claude controls (2026-10-21)

`gemini-3.8-flash` offers Low/Medium/High/Auto; legacy XHigh/Max selections
normalize to High. Both Claude 4.6 models offer Low/Medium/High/XHigh/Max/Auto;
Sonnet also offers Off. New native conversations default to Low. Gemini sends
`reasoning_effort`; Claude also sends adaptive thinking, or disabled for Off.
These fields use the existing streaming chat request and stable conversation
identity. Full history, tool results and native images retain their content.
Only compilation/type checking was run for this change; no tests or model
requests were run at the owner's request.

Native reasoning remains expanded when a streamed reply completes, respecting
manual collapse; reloaded history starts collapsed. Reasoning fragments update
as SSE frames arrive, without waiting for final content or a durable history
checkpoint. The generation reader also accepts CRLF event framing across
network chunk boundaries. Markdown output has no per-token animation delay.
