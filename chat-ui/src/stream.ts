export type ChatDelta = {
  content?: string;
  reasoning_content?: string;
  reasoning?: string;
};

// SSE is framed by blank lines, independent of network chunks and UTF-8 bytes.
export async function readChatStream(
  response: Response,
  onDelta: (delta: ChatDelta) => void,
) {
  if (!response.body) throw new Error("响应为空，请重试");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done = false;
  const consume = (event: string) => {
    const data = event
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    if (data.trim() === "[DONE]") {
      done = true;
      return;
    }
    const chunk = JSON.parse(data);
    if (chunk.error)
      throw new Error(chunk.error.message || "模型暂时无法响应，请重试");
    if (chunk.choices?.[0]?.delta) onDelta(chunk.choices[0].delta);
  };
  try {
    while (!done) {
      const next = await reader.read();
      buffer += decoder.decode(next.value, { stream: !next.done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary: number;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const event = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        consume(event);
      }
      if (next.done) {
        if (buffer.trim()) consume(buffer);
        if (!done) throw new Error("连接已中断，回复可能不完整，请重试");
        break;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
