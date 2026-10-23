import { Globe2, ExternalLink, ChevronDown, Search, LoaderCircle } from "lucide-react";
import { Tool, ToolHeader, ToolContent, ToolOutput } from "./components/ai-elements/tool";
import { Sources, SourcesTrigger, SourcesContent, Source } from "./components/ai-elements/sources";
import type { ToolCall } from "./chat-types";
import { searchSources, searchQueries, searchTime, toolState } from "./search-tool-state";

export default function SearchTool({ tool }: { tool: ToolCall }) {
  const sources = searchSources(tool);
  const queries = searchQueries(tool);
  const running = tool.status === "running";
  const time = searchTime(tool);
  const metadata = tool.metadata;
  const [showExtracts, setShowExtracts] = useState(false);
  return <Tool className="search-tool" defaultOpen data-tool-name="web_search" data-tool-status={tool.status}>
    <ToolHeader title="网页搜索" type="dynamic-tool" toolName="web_search" state={toolState(tool)}/>
    <ToolContent className="search-tool-content">
      {queries.length ? <div className="search-queries" aria-label="搜索词">{queries.map((query, index) => <div className="search-query" key={`${index}-${query.query}`}><Search size={14}/><div><p>{query.query}</p>{query.domains?.length ? <small>{query.domains.join(" · ")}</small> : null}{query.recency_days ? <small>最近 {query.recency_days} 天</small> : null}</div></div>)}</div> : null}
      <div className="search-result-status" role="status">{running ? <><LoaderCircle className="animate-spin" size={14}/>正在检索网页，结果会自动显示</> : tool.status === "failed" ? "搜索未完成" : <><Globe2 size={14}/>{sources.length ? `找到 ${sources.length} 个来源` : "未找到可引用的网页"}{tool.cached ? <span className="search-cache">缓存命中</span> : null}</>}</div>
      {metadata?.mode === "live" ? <p className="search-time">实时搜索{time ? <> · <time dateTime={metadata.searched_at}>检索于 {time}（北京时间）</time></> : null}</p> : null}
      {metadata?.truncated ? <p className="search-limit">{(metadata.total_sources || 0) > sources.length ? `显示 ${sources.length} / ${metadata.total_sources} 个来源。` : ""}部分检索内容达到返回上限。{metadata.truncated_fields?.includes("sources") ? "可提高 max_results 或缩小查询范围查看其余来源。" : ""}</p> : null}
      {metadata?.warnings?.map((warning, index) => <p className="search-limit" key={index}>{warning}</p>)}
      {tool.status !== "running" ? <ToolOutput errorText={tool.error} output={sources.length || tool.output ? <>
        {sources.length ? <Sources defaultOpen={sources.length <= 3} className="search-sources"><SourcesTrigger count={sources.length}><span>查看来源 · {sources.length}</span><ChevronDown size={14}/></SourcesTrigger><SourcesContent className="search-source-list">{sources.map(source => <article key={source.url} className="search-source-record"><Source href={source.url} title={source.title} className="search-source"><div className="search-source-heading"><span>{source.title}</span><ExternalLink size={13}/></div><small>{new URL(source.url).hostname}</small>{source.published_at || source.published ? <small>发布时间：{source.published_at || source.published}</small> : null}{source.crawled ? <small>抓取时间：{source.crawled}</small> : null}{source.snippet ? <p>{source.snippet}</p> : null}</Source>{source.content ? <details className="search-source-extract"><summary>查看来源摘录{source.content_truncated ? "（已达返回上限）" : ""}</summary><pre>{source.content}</pre></details> : null}</article>)}</SourcesContent></Sources> : null}
        {tool.output ? <details className="search-full-extract" onToggle={event => setShowExtracts(event.currentTarget.open)}><summary>查看检索内容</summary>{showExtracts ? <pre>{tool.output}</pre> : null}</details> : null}
      </> : undefined}/> : null}
      {metadata?.error?.suggestion ? <p className="search-limit">{metadata.error.suggestion}</p> : null}
    </ToolContent>
  </Tool>;
}
import { useState } from "react";
