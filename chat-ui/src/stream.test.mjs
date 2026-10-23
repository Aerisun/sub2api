import { test } from "node:test";
import assert from "node:assert/strict";
import { readChatStream } from "./stream.ts";
function response(bytes, width = 1) {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (let i = 0; i < bytes.length; i += width)
          controller.enqueue(bytes.slice(i, i + width));
        controller.close();
      },
    }),
  );
}
test("handles fragmented UTF-8, CRLF, reasoning, content, and usage-only events", async () => {
  const source =
    ': heartbeat\r\n\r\ndata: {"choices":[{"delta":{"reasoning_content":"思考"}}]}\r\n\r\ndata: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: {"choices":[],"usage":{"total_tokens":10}}\n\ndata: [DONE]\n\n';
  const deltas = [];
  await readChatStream(response(new TextEncoder().encode(source)), (delta) =>
    deltas.push(delta),
  );
  assert.deepEqual(deltas, [
    { reasoning_content: "思考" },
    { content: "你好" },
  ]);
});
test("surfaces upstream errors", async () => {
  await assert.rejects(
    readChatStream(
      response(
        new TextEncoder().encode('data: {"error":{"message":"余额不足"}}\n\n'),
      ),
      () => {},
    ),
    /余额不足/,
  );
});
test("does not present a truncated response as successful", async () => {
  await assert.rejects(
    readChatStream(
      response(
        new TextEncoder().encode(
          'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        ),
      ),
      () => {},
    ),
    /连接已中断/,
  );
});
