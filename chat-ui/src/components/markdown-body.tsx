import { ImagePreview } from "../image-attachments";
import { Streamdown, defaultRehypePlugins } from "streamdown";
import { useEffect, useState } from "react";
import { cjk } from "@streamdown/cjk";
import { rehypeImagePlaceholders } from "../rehype-image-placeholders";
import type { Components, ControlsConfig, PluginConfig, StreamdownTranslations } from "streamdown";
import "streamdown/styles.css";
import "katex/dist/katex.min.css";
import "../markdown.css";

// Render chat immediately. Large syntax/diagram engines load only when an
// actual message needs them, and failed optional downloads leave readable text.
let markdownPlugins: PluginConfig = { cjk };
const pluginLoads = new Map<string, Promise<void>>();
const loaders = {
  code: async () => ({ code: (await import("@streamdown/code")).code }),
  math: async () => ({ math: (await import("@streamdown/math")).createMathPlugin({ singleDollarTextMath: true }) }),
  mermaid: async () => ({ mermaid: (await import("@streamdown/mermaid")).mermaid }),
};
function loadPlugin(name: keyof typeof loaders): Promise<void> {
  let pending = pluginLoads.get(name);
  if (!pending) {
    pending = loaders[name]().then((plugin) => { markdownPlugins = { ...markdownPlugins, ...plugin }; });
    pluginLoads.set(name, pending);
    void pending.catch(() => { pluginLoads.delete(name); });
  }
  return pending;
}

const components: Components = {
  img: ({ src, alt }) => typeof src === "string" ? (
    <ImagePreview kind="generated" image={{ url: src, name: alt || "生成图片", mediaType: "image/png" }} />
  ) : null,
};
const rehypePlugins = [
  defaultRehypePlugins.raw,
  rehypeImagePlaceholders,
  defaultRehypePlugins.sanitize,
  defaultRehypePlugins.harden,
];
const controls: ControlsConfig = {
  code: { copy: true, download: false },
  table: false,
  mermaid: { copy: true, download: false, fullscreen: true, panZoom: true },
};
const translations: Partial<StreamdownTranslations> = {
  copyCode: "复制代码", copied: "已复制", copyTable: "复制表格",
  copyTableAsMarkdown: "复制为 Markdown", copyTableAsCsv: "复制为 CSV", copyTableAsTsv: "复制为 TSV",
  viewFullscreen: "全屏查看", exitFullscreen: "退出全屏", close: "关闭",
  zoomIn: "放大", zoomOut: "缩小", resetView: "重置视图",
  copyLink: "复制链接", openLink: "打开链接", openExternalLink: "打开外部链接",
  externalLinkWarning: "即将打开外部链接", imageNotAvailable: "图片暂时无法加载",
};

export function MarkdownBody({ children, className = "", isAnimating = false }: {
  children: string;
  className?: string;
  isAnimating?: boolean;
}) {
  const [plugins, setPlugins] = useState(markdownPlugins);
  const needsCode = /(?:^|\n)\s*(?:```|~~~)/.test(children);
  const needsMermaid = /(?:^|\n)\s*(?:```|~~~)mermaid\b/.test(children);
  const needsMath = /\$|\\\(|\\\[/.test(children);
  useEffect(() => {
    let active = true;
    const required: (keyof typeof loaders)[] = [];
    if (needsCode) required.push("code");
    if (needsMath) required.push("math");
    if (needsMermaid) required.push("mermaid");
    if (required.length) void Promise.allSettled(required.map(loadPlugin)).then(() => {
      if (active) setPlugins(markdownPlugins);
    });
    return () => { active = false; };
  }, [needsCode, needsMath, needsMermaid]);
  return (
    <div className={`markdown-body ${className}`} data-streaming={isAnimating || undefined}>
      <Streamdown
        className="markdown-prose"
        plugins={plugins}
        rehypePlugins={rehypePlugins}
        isAnimating={isAnimating}
        aria-busy={isAnimating}
        animated={false}
        caret="circle"
        lineNumbers={false}
        codeBlockMaxHeight={0}
        tableMaxHeight={0}
        controls={controls}
        translations={translations}
        components={components}
      >
        {children}
      </Streamdown>
    </div>
  );
}
