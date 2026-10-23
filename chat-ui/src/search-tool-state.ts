import type { ToolCall } from "./chat-types";

export function toolState(tool: ToolCall) {
  return tool.status === "running" ? "input-available" as const : tool.status === "failed" ? "output-error" as const : "output-available" as const;
}

export function searchQueries(tool: ToolCall) {
  const queries = tool.input?.queries || (tool.input?.query ? [{ query: tool.input.query }] : []);
  return queries.filter(query => typeof query?.query === "string" && query.query.trim()).slice(0, 4);
}

export function searchSources(tool: ToolCall) {
  const seen = new Set<string>();
  return (tool.sources || []).filter(source => {
    try {
      const url = new URL(source.url);
      if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || seen.has(url.href)) return false;
      seen.add(url.href);
      return true;
    } catch { return false; }
  }).slice(0, 50);
}

export function searchTime(tool: ToolCall) {
  const value = tool.metadata?.searched_at;
  if (!value || !Number.isFinite(Date.parse(value))) return null;
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value));
}
