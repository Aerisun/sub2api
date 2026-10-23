import { lazy, Suspense, useEffect, useState } from "react";
import { Check, ChevronRight, ImagePlus, ScanEye, LoaderCircle, CircleAlert, ArrowUpRight } from "lucide-react";
import { MessageResponse } from "./components/ai-elements/message";
import type { ToolCall } from "./chat-types";

const SearchTool = lazy(() => import("./search-tool"));

function ToolOutput({ tool }: { tool: ToolCall }) {
  const vision=tool.name === "image_recognition";
  const [open,setOpen]=useState(vision || tool.status === "running");
  useEffect(()=>{if(!vision && tool.status !== "running")setOpen(false);},[tool.status,vision]);
  const label=vision ? `图片识别${tool.input?.batchCount ? ` · 图片 ${tool.input.batchIndex}/${tool.input.batchCount}` : tool.input?.images?.length ? ` · ${tool.input.images.length} 张` : ""}` : "图片生成";
  const status=tool.status === "running" ? (vision ? "正在识别…" : "正在生成…") : tool.status === "failed" ? "处理失败" : tool.cached ? "已复用识图结果" : "已完成";
  const Icon=tool.status === "running" ? LoaderCircle : tool.status === "failed" ? CircleAlert : Check;
  // The actual generated image is displayed in the answer. Keep its nested
  // tool result available without rendering the same large image twice.
  const output=vision ? tool.output : tool.output?.replace(/!\[([^\]]*)\]\(([^)]+)\)/g,"[查看生成的图片]($2)");
  const links=!vision ? [...new Set([...(output || "").matchAll(/\[[^\]]*\]\((https:\/\/[^)\s]+)\)/g)].map(match=>match[1]))] : [];
  return <details className="internal-tool" data-tool-id={tool.id} data-tool-name={tool.name} data-tool-status={tool.status} open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary><ChevronRight className="tool-chevron" size={14}/>{vision ? <ScanEye size={16}/> : <ImagePlus size={16}/>}<span className="tool-name" title={vision ? tool.input?.images?.map(image=>image.name).join("、") : undefined}>{label}</span>{!vision && tool.metadata?.model ? <code className="tool-model">{tool.metadata.model}</code> : null}<span className="tool-status"><Icon size={13} className={tool.status === "running" ? "animate-spin" : ""}/>{status}</span></summary>
    <div className="tool-output" aria-live="polite" aria-atomic="false">{output ? vision ? <MessageResponse isAnimating={tool.status === "running"}>{output}</MessageResponse> : links.length ? <div className="tool-result-links">{links.map((url,index)=><a href={url} target="_blank" rel="noopener noreferrer" key={url}><ImagePlus size={14}/>{links.length>1 ? `查看图片 ${index+1}` : "查看图片"}<ArrowUpRight size={13}/></a>)}</div> : <p>{output}</p> : <p className="tool-wait">{tool.status === "failed" ? "工具调用未完成。" : vision ? "正在读取图片内容，识别结果会逐步显示。" : "正在生成图片，完成后将显示实际结果。"}</p>}{tool.error ? <p className="message-error">{tool.error}</p> : null}</div>
  </details>;
}
export function ToolCalls({tools}:{tools:ToolCall[]}) {
 const visible=visibleToolCalls(tools);
 if (!visible.length) return null;
 if(visible.length>1 && visible.every(tool=>tool.name === "image_recognition" && (tool.input?.batchCount || 0)>1 && tool.input?.batchCount===visible[0].input?.batchCount))visible.sort((a,b)=>(a.input?.batchIndex || 0)-(b.input?.batchIndex || 0));
 return <div className="internal-tools" aria-label="工具调用">{visible.map(tool=>tool.name === "web_search" ? <Suspense key={tool.id} fallback={<div className="tool-wait">正在加载网页搜索结果…</div>}><SearchTool tool={tool}/></Suspense> : <ToolOutput key={tool.id} tool={tool}/>)}</div>;
}

export function ImageGenerationModels({ tools }: { tools: ToolCall[] }) {
 const models = [...new Set(tools.filter(tool => tool.name === "image_generation" && tool.status === "completed").map(tool => tool.metadata?.model || "未记录"))];
 if (!models.length) return null;
 return <div className="image-generation-models" aria-label="图片生成模型">{models.map(model => <span className="image-generation-model" key={model}><ImagePlus size={13} aria-hidden="true"/><span>生图模型</span><code>{model}</code></span>)}</div>;
}

export function visibleToolCalls(tools: ToolCall[]) {
 return tools.filter(tool => !(tool.name === "image_recognition" && tool.cached));
}
