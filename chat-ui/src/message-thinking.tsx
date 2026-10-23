import { useEffect,useRef,useState } from "react";
import type { ReactNode } from "react";
import { Reasoning,ReasoningTrigger,ReasoningContent } from "./components/ai-elements/reasoning";
import { MessageResponse } from "./components/ai-elements/message";
import { ToolCalls, visibleToolCalls } from "./tool-calls";
import type { ToolCall } from "./chat-types";
import { formatGeminiReasoning } from "./reasoning-format";

export function InlineToolTimeline({text,tools,offsetKey,isAnimating=false,formatText=(value:string)=>value}:{text:string;tools:ToolCall[];offsetKey:"contentOffset"|"reasoningOffset";isAnimating?:boolean;formatText?:(text:string)=>string}) {
 const positioned=tools.map((tool,index)=>({tool,index,offset:Math.max(0,Math.min(text.length,Array.from(text).slice(0,tool[offsetKey] || 0).join("").length))})).sort((a,b)=>a.offset-b.offset);
 if(!positioned.length) return text ? <MessageResponse isAnimating={isAnimating}>{formatText(text)}</MessageResponse> : null;
 const result=[] as ReactNode[];
 let cursor=0;
 for(let i=0;i<positioned.length;) {
  const offset=positioned[i].offset;
  if(offset>cursor) result.push(<MessageResponse key={`text-${cursor}`} isAnimating={isAnimating}>{formatText(text.slice(cursor,offset))}</MessageResponse>);
  const group=[] as ToolCall[];
  while(i<positioned.length && positioned[i].offset===offset) group.push(positioned[i++].tool);
  result.push(<ToolCalls key={`tools-${offset}`} tools={group}/>);
  cursor=offset;
 }
 if(cursor<text.length) result.push(<MessageResponse key={`text-${cursor}`} isAnimating={isAnimating}>{formatText(text.slice(cursor))}</MessageResponse>);
 return <>{result}</>;
}

export function MessageThinking({reasoning,tools=[],isStreaming,model=""}:{reasoning?:string;tools?:ToolCall[];isStreaming:boolean;model?:string}) {
 tools=visibleToolCalls(tools);
 const searches=tools.filter(tool=>tool.name === "web_search").length;
 const [open,setOpen]=useState(isStreaming || searches > 0);
 const userChanged=useRef(false);
 const previousStreaming=useRef(isStreaming);
 const hadSearch=useRef(searches > 0);
 useEffect(()=>{
  if(isStreaming && !previousStreaming.current) {userChanged.current=false;setOpen(true);}
  if(searches > 0 && !hadSearch.current && !userChanged.current) setOpen(true);
  previousStreaming.current=isStreaming;
  hadSearch.current=searches > 0;
 },[isStreaming,searches]);
 if (!reasoning && !tools.length) return null;
 const recognizing=tools.some(tool=>tool.name === "image_recognition" && tool.status === "running");
 const generating=tools.some(tool=>tool.name === "image_generation" && tool.status === "running");
 const searching=tools.some(tool=>tool.name === "web_search" && tool.status === "running");
 const codex=/^gpt-(6|5\.6)/.test(model);
 return <Reasoning open={open} onOpenChange={(value)=>{userChanged.current=true;setOpen(value);}} defaultOpen={false} isStreaming={isStreaming} data-message-thinking="true">
  <ReasoningTrigger getThinkingMessage={(streaming,duration)=>streaming ? (searching ? "正在搜索网页…" : recognizing ? "正在识图…" : generating ? "正在生成图片…" : "正在思考…") : searches ? `${codex ? "思考摘要" : "思考过程"} · 网页搜索 ${searches} 次` : duration ? (codex ? `思考摘要 · ${duration} 秒` : `思考了 ${duration} 秒`) : codex ? "思考摘要" : "思考过程"}/>
  <ReasoningContent>
   <InlineToolTimeline text={reasoning || ""} tools={tools.filter(tool => tool.name === "web_search" || tool.name === "image_recognition" || !tool.contentOffset)} offsetKey="reasoningOffset" isAnimating={isStreaming} formatText={model.startsWith("gemini-web-") ? formatGeminiReasoning : undefined}/>
  </ReasoningContent>
 </Reasoning>;
}
