import { useEffect, useState } from "react";
import type { ComponentType } from "react";

type MarkdownProps = { children: string; className?: string; isAnimating?: boolean };
let renderer: ComponentType<MarkdownProps> | undefined;
let loading: Promise<void> | undefined;

// Composer and first real tokens do not depend on the full Markdown engine.
// An optional engine failure leaves the original text visible.
export function preloadMarkdown(): Promise<void> {
  if (!loading) {
    loading = import("./rich-markdown-body").then(module => {
      renderer = module.RichMarkdownBody;
    }).catch(error => {
      loading = undefined;
      throw error;
    });
  }
  return loading;
}

export function MarkdownBody(props: MarkdownProps) {
  const [Renderer, setRenderer] = useState(() => renderer);
  useEffect(() => {
    let active = true;
    void preloadMarkdown().then(() => {
      if (active) setRenderer(() => renderer);
    }).catch(() => {});
    return () => { active = false; };
  }, []);
  return Renderer ? <Renderer {...props} /> : (
    <div className={`markdown-body whitespace-pre-wrap break-words ${props.className || ""}`}
      data-streaming={props.isAnimating || undefined}>{props.children}</div>
  );
}
