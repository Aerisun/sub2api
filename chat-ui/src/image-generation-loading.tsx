import { ImageIcon, Sparkles } from "lucide-react";
import { useReducedMotion } from "motion/react";
import { Shimmer } from "./components/ai-elements/shimmer";

// The official AI Elements Shimmer supplies the loading label. The preview is
// intentionally indeterminate: the image provider does not publish a percentage.
export function ImageGenerationLoading({phase="generating"}:{phase?:"generating"|"loading"}) {
 const reducedMotion=useReducedMotion();
 const label=phase === "loading" ? "正在加载图片" : "正在生成图片";
 return <span className="image-generation-loading" role="status" aria-label={label} data-image-generation-loading={phase}>
  <span className="generation-preview-art" aria-hidden="true"><ImageIcon size={35} strokeWidth={1.25}/><Sparkles className="generation-spark" size={15}/></span>
  {reducedMotion ? <span className="generation-loading-label">{label}</span> : <Shimmer as="span" duration={2.3} className="generation-loading-label">{label}</Shimmer>}
  {phase === "loading" ? <span className="generation-loading-hint">即将展示完整画面</span> : null}
 </span>;
}
