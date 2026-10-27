export type Effort =
  | "default"
  | "auto"
  | "none"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max";
export type ModelProfile = {
  brand: string;
  mark: string;
  color: string;
  efforts: Effort[];
  alwaysThinking: boolean;
  note?: string;
  family: "codex" | "claude" | "gemini" | "qwen" | "glm52" | "glm53" | "kimi" | "deepseek" | "nemotron" | "gemini-web" | "unknown";
};
export const effortDetails: Record<
  Effort,
  { label: string; description: string }
> = {
  default: { label: "", description: "" },
  auto: { label: "自动", description: "由模型分配思考预算" },
  none: { label: "关闭思考", description: "直接回答，不生成思考过程" },
  low: { label: "低", description: "更快响应，适合简单问答" },
  medium: { label: "中", description: "平衡响应速度与思考深度" },
  high: { label: "高", description: "深入分析，适合复杂问题" },
  xhigh: { label: "超高", description: "充分推理，适合代码与难题" },
  max: { label: "最大", description: "最深入推理，耗时可能增加" },
};
// Native provider tiers, rather than every alias accepted by the gateway.
// Sources and the gateway's provider-specific mappings are recorded in README.
export function modelProfile(model: string = ""): ModelProfile {
  const name = model.toLowerCase().split("/").at(-1) || "";
  if (/^claude-(?:sonnet-4-6|opus-4-6-thinking)$/.test(name)) {
    const alwaysThinking = name === "claude-opus-4-6-thinking";
    return {
      brand: "Claude", mark: "C", color: "claude", family: "claude",
      efforts: [...(alwaysThinking ? [] : ["none" as Effort]), "low", "medium", "high", "xhigh", "max", "auto"],
      alwaysThinking,
    };
  }
  if (name === "gemini-3.8-flash")
    return {
      brand: "Gemini", mark: "G", color: "gemini", family: "gemini",
      efforts: ["low", "medium", "high", "auto"],
      alwaysThinking: true,
    };
  if (/^gpt-(6(?:\.1)?-(?:astra|sol|luna)|6\.1-terra|5\.6-(?:sol|terra|luna))(?:-|$)/.test(name)) {
    const alwaysThinking = /^gpt-(6-astra|6\.1-sol)(?:-|$)/.test(name);
    return {
      brand: "OpenAI", mark: "O", color: "openai", family: "codex",
      efforts: [...(alwaysThinking ? [] : ["none" as Effort]), "low", "medium", "high", "xhigh", "max"],
      alwaysThinking,
      note: "显示模型返回的思考摘要；较高强度可能耗时更长。",
    };
  }
  if (/^gemini-web-(flash-lite|flash|pro|argon)$/.test(name))
    return {
      brand: "Gemini",
      mark: "G",
      color: "gemini",
      efforts: ["low", "high"],
      alwaysThinking: true,
      family: "gemini-web",
      note: "标准思考更快；扩展思考适合复杂问题。",
    };
  if (/^qwen3\.8-(flash|max)(?:-|$)/.test(name))
    return {
      brand: "Qwen",
      mark: "Q",
      color: "qwen",
      efforts: ["none", "low", "medium", "xhigh"],
      alwaysThinking: false,
      family: "qwen",
    };
  if (/^glm-5\.2(?:-|$)/.test(name))
    return {
      brand: "GLM",
      mark: "G",
      color: "glm",
      efforts: ["none", "high", "max"],
      alwaysThinking: false,
      family: "glm52",
    };
  if (/^glm-5\.3(?:-|$)/.test(name))
    return {
      brand: "GLM",
      mark: "G",
      color: "glm",
      efforts: ["low", "high", "max"],
      alwaysThinking: true,
      family: "glm53",
    };
  if (/^kimi-k3(?:-|$)/.test(name))
    return {
      brand: "Kimi",
      mark: "K",
      color: "kimi",
      efforts: ["low", "high", "max"],
      alwaysThinking: true,
      family: "kimi",
    };
  if (/^deepseek-v4\.1-flash(?:-|$)/.test(name))
    return {
      brand: "DeepSeek",
      mark: "D",
      color: "deepseek",
      efforts: ["none", "low", "high", "max"],
      alwaysThinking: false,
      family: "deepseek",
    };
  if (/^nemotron-3/.test(name)) return {
    brand: "NVIDIA", mark: "N", color: "nvidia",
    efforts: ["none", "low", "medium", "high", "xhigh", "max"],
    alwaysThinking: false, family: "nemotron",
    note: "通过思考预算调节深度，较高档位可能耗时更长。",
  };
  if (/^step-5(?:-|$)/.test(name)) return {
    brand: "StepFun", mark: "S", color: "step", efforts: [],
    alwaysThinking: true, family: "unknown",
    note: "此模型始终思考，当前渠道未开放强度调节。",
  };
  return {
    brand: model.split("-")[0] || "AI",
    mark: "✦",
    color: "other",
    efforts: [],
    alwaysThinking: false,
    family: "unknown",
  };
}
export function validEffort(model: string, value: string | undefined): Effort {
  const profile = modelProfile(model);
  if (profile.efforts.includes(value as Effort)) return value as Effort;
  if (profile.family === "gemini" && (value === "xhigh" || value === "max")) return "high";
  // Resolve legacy/default selections to an explicit supported tier. The UI
  // displays this same value and the request always sends it.
  const initial: Partial<Record<ModelProfile["family"], Effort>> = {
    codex: "medium", claude: "low", gemini: "low",
    qwen: "xhigh", glm52: "max", glm53: "low", kimi: "low",
    deepseek: "low", nemotron: "low", "gemini-web": "low",
  };
  if (model.toLowerCase().split("/").at(-1) === "gpt-6.1-terra") return "low";
  return initial[profile.family] || "default";
}
export function reasoningParams(
  model: string,
  value: string | undefined,
): Record<string, unknown> {
  const effort = validEffort(model, value);
  if (effort === "default") return {};
  const profile = modelProfile(model);
  if (profile.family === "claude")
    return { reasoning_effort: effort, thinking: { type: effort === "none" ? "disabled" : "adaptive" } };
  if (profile.family === "qwen")
    return { reasoning_effort: effort, enable_thinking: effort !== "none" };
  if (profile.family === "codex" || profile.family === "kimi" || profile.family === "gemini" || profile.family === "gemini-web")
    return { reasoning_effort: effort };
  return {
    reasoning_effort: effort,
    thinking: { type: effort === "none" ? "disabled" : "enabled" },
  };
}

export function chatParams(model: string, effort: string | undefined, fast = false): Record<string, unknown> {
  return {
    ...reasoningParams(model, effort),
    ...(fast && modelProfile(model).family === "codex" ? { service_tier: "fast" } : {}),
  };
}
