import { useEffect, useState } from "react";

// A selected reasoning tier is not proof that the provider is sending thoughts.
// Until real content arrives, describe the wait and the reader's connection.
export function GenerationWait({ preparing, connection, hasReasoning, tool }: {
  preparing: boolean;
  connection?: "connecting" | "connected" | "reconnecting";
  hasReasoning: boolean;
  tool?: "web_search" | "image_recognition";
}) {
  const [started] = useState(Date.now);
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [started]);
  const label = connection === "reconnecting" ? "回复连接中断，正在重新连接…"
    : tool === "web_search" ? "正在搜索网页…"
    : tool === "image_recognition" ? "正在识图…"
    : hasReasoning ? "正在生成回答…"
    : preparing ? "正在发送消息…"
    : seconds >= 8 ? "模型暂未返回内容…"
    : "正在等待模型响应…";
  return <div className="thinking" role="status" aria-live="polite">
    <span>{label}</span>{seconds > 0 ? <span className="generation-wait-time" aria-live="off"> · {seconds} 秒</span> : null}
  </div>;
}
