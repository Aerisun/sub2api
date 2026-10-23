import { Brain, Zap } from "lucide-react";
import {
  PromptInputSelect,
  PromptInputSelectTrigger,
  PromptInputSelectContent,
  PromptInputSelectItem,
} from "@/components/ai-elements/prompt-input";
import { SelectGroup, SelectLabel } from "@/components/ui/select";
import { effortDetails, modelProfile, type Effort } from "./model-settings";
import { providerIcons, type ProviderIconKey } from "./provider-icons";

const brandIconKeys: Record<string, ProviderIconKey> = {
  OpenAI: "openai",
  NVIDIA: "nvidia",
  DeepSeek: "deepseek",
  GLM: "zhipu",
  Kimi: "kimi",
  Qwen: "qwen",
  StepFun: "stepfun",
};

export function BrandIcon({ brand, color }: { brand: string; color: string }) {
  const iconKey = brandIconKeys[brand];
  const icon = iconKey ? providerIcons[iconKey] : undefined;
  return (
    <span
      className={`brand-mark ${color}`}
      style={icon ? { color: icon.color } : undefined}
    >
      {icon ? (
        <svg
          viewBox="0 0 24 24"
          fill="currentColor"
          fillRule="evenodd"
          aria-hidden="true"
          focusable="false"
        >
          {icon.paths.map((path, index) => (
            <path key={index} d={path} />
          ))}
        </svg>
      ) : (
        brand.slice(0, 1)
      )}
    </span>
  );
}

type Props = {
  model: string;
  models: string[];
  effort: Effort;
  fast: boolean;
  disabled: boolean;
  loading: boolean;
  onModel: (value: string) => void;
  onEffort: (value: Effort) => void;
  onFast: (value: boolean) => void;
};
export function ModelControls({
  model,
  models,
  effort,
  fast,
  disabled,
  loading,
  onModel,
  onEffort,
  onFast,
}: Props) {
  const profile = modelProfile(model);
  const details = (value: Effort) => profile.family === "gemini-web"
    ? { label: value === "low" ? "标准思考" : "扩展思考",
        description: value === "low" ? "更快响应，适合日常问答" : "延长思考，适合代码与复杂问题" }
    : effortDetails[value];
  const groups = Array.from(new Set(models.map((m) => modelProfile(m).brand)));
  return (
    <div className="model-controls">
      <PromptInputSelect
        value={model || undefined}
        disabled={disabled || loading || !models.length}
        onValueChange={(value) => {
          // PromptInput resets its form after send. Ignore the hidden select's
          // reset event so a loaded model is not replaced with undefined.
          if (models.includes(value)) onModel(value);
        }}
      >
        <PromptInputSelectTrigger
          className="model-trigger"
          aria-label="选择模型"
        >
          <BrandIcon brand={profile.brand} color={profile.color} />
          <span className="model-trigger-name">
            {loading ? "加载模型…" : model || "暂无可用模型"}
          </span>
        </PromptInputSelectTrigger>
        <PromptInputSelectContent
          position="popper"
          side="top"
          align="start"
          sideOffset={10}
          className="model-menu settings-menu"
        >
          {groups.map((brand) => (
            <SelectGroup key={brand}>
              <SelectLabel className="brand-group-label">{brand}</SelectLabel>
              {models
                .filter((m) => modelProfile(m).brand === brand)
                .map((m) => (
                  <PromptInputSelectItem
                    key={m}
                    value={m}
                    className="model-menu-item"
                  >
                    <span className="model-option">
                      <BrandIcon
                        brand={brand}
                        color={modelProfile(m).color}
                      />
                      <span>{m}</span>
                    </span>
                  </PromptInputSelectItem>
                ))}
            </SelectGroup>
          ))}
        </PromptInputSelectContent>
      </PromptInputSelect>
      {profile.efforts.length > 0 && (
        <>
          <span className="controls-divider" aria-hidden="true" />
          <PromptInputSelect
            value={effort}
            disabled={disabled}
            onValueChange={(value) => {
              if (profile.efforts.includes(value as Effort))
                onEffort(value as Effort);
            }}
          >
            <PromptInputSelectTrigger
              className={`effort-trigger ${effort !== "default" && effort !== "none" ? "effort-active" : ""}`}
              aria-label="思考强度"
            >
              <Brain className="size-3.5" />
              <span className="effort-prefix">思考</span>
              <span className="effort-value">
                {details(effort).label}
              </span>
            </PromptInputSelectTrigger>
            <PromptInputSelectContent
              position="popper"
              side="top"
              align="start"
              sideOffset={10}
              className="effort-menu settings-menu"
            >
              {profile.efforts.map((value) => (
                <PromptInputSelectItem
                  key={value}
                  value={value}
                  className="effort-menu-item"
                >
                  <span className="effort-option">
                    <span className="effort-option-title">
                      {details(value).label}
                      <span
                        className={`effort-meter level-${value}`}
                        aria-hidden="true"
                      >
                        {[0, 1, 2, 3].map((n) => (
                          <i key={n} />
                        ))}
                      </span>
                    </span>
                    <span className="effort-description">
                      {details(value).description}
                    </span>
                  </span>
                </PromptInputSelectItem>
              ))}
            </PromptInputSelectContent>
          </PromptInputSelect>
        </>
      )}
      {profile.family === "codex" && (
        <button type="button" className={`fast-toggle${fast ? " active" : ""}`}
          disabled={disabled || loading} aria-label="Fast 模式，2 倍费用" aria-pressed={fast}
          title={fast ? "Fast 已开启 · 2 倍费用，点击关闭" : "开启 Fast · 2 倍费用"}
          onClick={() => onFast(!fast)}>
          <Zap className="size-4" fill={fast ? "currentColor" : "none"} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}
