import { ImageGenerationLoading } from "./image-generation-loading";
import { GenerationWait } from "./generation-wait";
import { ImageGenerationModels } from "./tool-calls";
import { InlineToolTimeline, MessageThinking } from "./message-thinking";
import { createRoot } from "react-dom/client";
import { useEffect, useRef, useState, type SetStateAction } from "react";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import {
  Message,
  MessageContent,
  MessageResponse,
  MessageActions,
  MessageAction,
} from "@/components/ai-elements/message";
import {
  PromptInput,
  PromptInputTextarea,
  PromptInputSubmit,
  PromptInputFooter,
  PromptInputTools,
  type PromptInputMessage,
  type PromptInputAttachment,
  usePromptInputAttachments,
} from "@/components/ai-elements/prompt-input";
import { Suggestion } from "@/components/ai-elements/suggestion";
import { Button } from "@/components/ui/button";
import {
  Copy,
  Check,
  Plus,
  MessageSquare,
  LoaderCircle,
  PanelLeft,
  PanelLeftClose,
  PanelLeftOpen,
  Trash2,
  RotateCcw,
  Sparkles,
  BookOpen,
  ImagePlus,
} from "lucide-react";
import { ImageAttachments, UploadImageButton, ImageGallery } from "./image-attachments";
import { type ChatImage, persistImages, persistRecognitionImages, recognitionImageInputs, supportsNativeImages } from "./images";
import { useChatHistory } from "./use-chat-history";
import type { Item, Chat } from "./chat-types";
import { historyRequest } from "./history-api";
import "./style.css";
import { BrandIcon, ModelControls } from "./model-controls";
import {
  validEffort,
  chatParams,
  effortDetails,
  modelProfile,
  type Effort,
} from "./model-settings";

const id = () => crypto.randomUUID();
const welcomeSuggestions = [
  {
    title: "解释一个学习术语",
    description: "用生活中的例子理解「过拟合」",
    prompt: "请用通俗的语言解释机器学习中的「过拟合」，举一个生活中的例子，并说明如何避免。",
    icon: BookOpen,
  },
  {
    title: "生成一张图片",
    description: "画一只在月球上读书的小猫",
    prompt: "请生成一张图片：一只小猫坐在月球上读书，远处是蓝色地球，柔和的光线，温暖梦幻的插画风格。",
    icon: ImagePlus,
  },
];
const apiBase = `${location.pathname.split("/chat-ui/")[0]}/api/v1`;
function identity() {
  try {
    return JSON.parse(localStorage.getItem("auth_user") || "null")?.id;
  } catch {
    return null;
  }
}
const userID = identity();
const storageKey = `aerisun:chat:${userID}`;
function headers() {
  return {
    Authorization: `Bearer ${localStorage.getItem("auth_token") || ""}`,
    "Content-Type": "application/json",
  };
}
async function api(path: string, init?: RequestInit) {
  const response = await fetch(apiBase + path, { ...init, headers: headers() });
  if (!response.ok) {
    let message =
      response.status === 401
        ? "登录已过期，请返回页面重新登录"
        : response.status === 402
          ? "余额不足，请充值后继续"
          : response.status === 429
            ? "请求较多，请稍后重试"
            : "请求失败，请稍后重试";
    try {
      const body = await response.json();
      message = body.error?.message || body.message || message;
    } catch {}
    throw new Error(message);
  }
  return response;
}
function App() {
  const [preparing, setPreparing] = useState<ReadonlySet<string>>(() => new Set());
  const [error, setError] = useState("");
  const {chats,setChats,current,setCurrent,ready:historyReady,deleteChat,refresh,detailLoading,acceptServer,beginSend,finishSend} = useChatHistory(storageKey,userID,preparing,setError);
  const [models, setModels] = useState<string[]>([]);
  const [draftID, setDraftID] = useState(id);
  const composerID = current || draftID;
  const [drafts, setDrafts] = useState<Record<string, { text?: string; model?: string; fast?: boolean; files?: PromptInputAttachment[]; error?: string }>>({});
  const draftsRef = useRef(drafts); draftsRef.current = drafts;
  const activeComposer = useRef(composerID); activeComposer.current = composerID;
  const draft = drafts[composerID];
  function patchDraft(chatID: string, patch: Partial<NonNullable<typeof draft>>) {
    setDrafts((all) => ({ ...all, [chatID]: { ...all[chatID], ...patch } }));
  }
  const input = draft?.text || "";
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const setInput = (text: string) => patchDraft(composerID, { text });
  const setModel = (model: string) => patchDraft(composerID, { model, fast: false });
  const setFiles = (action: SetStateAction<PromptInputAttachment[]>) => setDrafts((all) => ({
    ...all, [composerID]: { ...all[composerID], files: typeof action === "function" ? action(all[composerID]?.files || []) : action },
  }));
  const selected = chats.find((chat) => chat.id === current);
  const model = [draft?.model, selected?.model].find((value): value is string => !!value && models.includes(value)) || models[0] || "";
  const uploadLimit = supportsNativeImages(model) ? 4 : 12;
  const [efforts, setEfforts] = useState<Record<string, Effort>>(() => {
    try {
      return JSON.parse(localStorage.getItem(storageKey + ":efforts") || "{}");
    } catch {
      return {};
    }
  });
  const effort = validEffort(model, efforts?.[model]);
  const fast = modelProfile(model).family === "codex" && !!draft?.fast;
  const [fastNotice, setFastNotice] = useState(false);
  useEffect(() => {
    if (!fastNotice) return;
    const timer = setTimeout(() => setFastNotice(false), 3500);
    return () => clearTimeout(timer);
  }, [fastNotice]);
  useEffect(() => {
    try {
      localStorage.setItem(storageKey + ":efforts", JSON.stringify(efforts));
    } catch {}
  }, [efforts]);
  const [menu, setMenu] = useState(false);
  const [historyCollapsed, setHistoryCollapsed] = useState(false);
  const [copied, setCopied] = useState("");
  const [loadingModels, setLoadingModels] = useState(true);
  const [reload, setReload] = useState(0);
  const sending = useRef(new Set<string>());
  const pendingGenerations = useRef(new Map<string, { id: string; stopRequested: boolean }>());
  const [stopping, setStopping] = useState<ReadonlySet<string>>(() => new Set());
  function markStopping(chatID: string, value: boolean) {
    setStopping(all => { const next = new Set(all); if (value) next.add(chatID); else next.delete(chatID); return next; });
  }
  const mounted = useRef(true);
  const latestChats = useRef(chats); latestChats.current = chats;
  const messages = selected?.messages || [];
  const busy = preparing.has(composerID) || selected?.generation?.status === "running";
  const composerError = draft?.error || error;
  useEffect(() => {
    const syncTheme = () =>
      document.documentElement.classList.toggle(
        "dark",
        window.parent.document.documentElement.classList.contains("dark"),
      );
    syncTheme();
    const observer = new MutationObserver(syncTheme);
    observer.observe(window.parent.document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    const onStorage = () => {
      if (identity() !== userID || !localStorage.getItem("auth_token")) {
        setChats([]);
        setError("登录状态已改变，请刷新页面");
      }
    };
    window.addEventListener("storage", onStorage);
    return () => {
      mounted.current = false;
      for (const draft of Object.values(draftsRef.current)) {
        for (const file of draft.files || []) URL.revokeObjectURL(file.url);
      }
      observer.disconnect();
      window.removeEventListener("storage", onStorage);
    };
  }, []);
  useEffect(() => {
    setLoadingModels(true);
    api("/chat/models")
      .then((r) => r.json())
      .then((body) => {
        const available = (body.data || [])
          .map((m: { id: string }) => m.id)
          .filter(
            (m: string) =>
              !/embedding|rerank|image|tts|whisper|sora|video|^web_search$/i.test(m),
          )
          .sort();
        setModels(available);
        setError(available.length ? "" : "暂无可用的对话模型");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoadingModels(false));
  }, [reload]);
  function updateMessages(
    chatID: string,
    transform: (items: Item[]) => Item[],
  ) {
    if (!mounted.current) return;
    setChats((all) =>
      all.map((chat) =>
        chat.id === chatID
          ? { ...chat, messages: transform(chat.messages), updated: Date.now() }
          : chat,
      ),
    );
  }
  async function send(text: string, retry = false, files: PromptInputMessage["files"] = []) {
    text = text.trim();
    const chatID = composerID;
    if (busy || sending.current.has(chatID) || latestChats.current.find(c => c.id === chatID)?.generation?.status === "running" || !historyReady || detailLoading || !model || (!text && !files.length && !retry) || identity() !== userID)
      throw new Error("当前无法发送消息");
    if (!retry && files.length > uploadLimit) {
      patchDraft(chatID,{error:`本轮最多上传 ${uploadLimit} 张图片`});
      throw new Error("图片数量超过限制");
    }
    const images: ChatImage[] = files.map((file) => ({ url: file.url, name: file.filename || "图片", mediaType: file.mediaType }));
    if (images.some((image) => !/^data:image\/(png|jpeg|webp);base64,/.test(image.url || ""))) {
      patchDraft(chatID, { error: "图片读取失败，请重新上传 PNG、JPG 或 WebP 图片" });
      throw new Error("图片读取失败");
    }
    const previous = retry
      ? messages.slice(0, messages.map((m) => m.role).lastIndexOf("user") + 1)
      : [...messages, { id: id(), role: "user" as const, content: text, images }];
    const baseVersion=beginSend(chatID);
    sending.current.add(chatID);
    setPreparing(new Set(sending.current));
    setError("");
    setDrafts(all => ({ ...all, [chatID]: { ...all[chatID], model, text: all[chatID]?.text?.trim() === text ? "" : all[chatID]?.text, error: "" } }));
    setMenu(false);
    const assistant: Item = {
      id: id(),
      role: "assistant",
      content: "",
      reasoning: "",
      model,
      effort,
      serviceTier: fast ? "fast" : undefined,
    };
    const pending = { id: assistant.id, stopRequested: false };
    pendingGenerations.current.set(chatID, pending);
    if (!current) {
      if (activeComposer.current === chatID) setCurrent(chatID);
      setChats((all) =>
        [
          {
            id: chatID,
            title: text.slice(0, 28) || "图片对话",
            messages: [...previous, assistant],
            model,
            updated: Date.now(),
          },
          ...all,
        ].slice(0, 50),
      );
    } else updateMessages(chatID, () => [...previous, assistant]);
    const snapshot: Chat = {
      id:chatID,title:selected?.title || text.slice(0,28) || "图片对话",model,
      version:baseVersion,updated:Date.now(),messages:[...previous,assistant],
    };
    try {
      const append=!!snapshot.version;
      const outgoing=append ? snapshot.messages.slice(-2) : snapshot.messages;
      // Text turns reach fetch synchronously. Only images needed by this turn
      // can delay it; old transcript synchronization is independent.
      const uploaded=supportsNativeImages(model)
        ? outgoing.some(m=>m.images?.some(image=>!image.assetID)) ? await persistImages(outgoing) : outgoing
        : await persistRecognitionImages(outgoing);
      const persisted=append ? [...snapshot.messages.slice(0,-2),...uploaded] : uploaded;
      const imageInputs=await recognitionImageInputs(persisted);
      if (pending.stopRequested) {
        updateMessages(chatID, items => items.map(item => item.id === assistant.id ? { ...item, error: "已停止生成" } : item));
        return;
      }
      const payload={requestID:assistant.id,append,retry,imageInputs,
        base:{messageCount:messages.length,lastMessageID:messages.at(-1)?.id || ""},
        params:chatParams(model,effort,fast),chat:{...snapshot,messages:uploaded.map(m=>({...m,images:m.images?.map(({url,...image})=>image)}))}};
      let accepted: Chat;
      try {
        const response=await historyRequest(`/conversations/${chatID}/generations`,{method:"POST",body:JSON.stringify(payload)});
        accepted=(await response.json()).data;
        if(!accepted.messages?.length)accepted.messages=persisted;
      } catch(e) {
        // A lost HTTP response is recovered by its durable request ID, never by
        // launching another model request.
        const recovery=await historyRequest(`/conversations/${chatID}`).then(r=>r.json()).catch(()=>null);
        if(recovery?.data?.generation?.id!==assistant.id)throw e;
        accepted=recovery.data;
      }
      if(mounted.current)acceptServer(accepted);
      if (pending.stopRequested) await stopChatGeneration(chatID, assistant.id);
    } catch(e) {
      const message=(e as Error).message;
      updateMessages(chatID,items=>items.map(item=>item.id===assistant.id?{...item,error:message}:item));
      patchDraft(chatID, { error: message });
      throw e;
    } finally {
      finishSend(chatID);
      sending.current.delete(chatID);
      pendingGenerations.current.delete(chatID);
      if(mounted.current) { setPreparing(new Set(sending.current)); markStopping(chatID, false); }
    }
  }
  async function stopChatGeneration(chatID: string, generationID: string) {
    await historyRequest(`/generations/${generationID}/stop`, { method: "POST" });
    // Confirm the durable state directly; an SSE reconnect must not be required
    // for the stop button to finish or to recover an orphaned pre-restart job.
    for (let attempt = 0; attempt < 20; attempt++) {
      const latest: Chat = (await historyRequest(`/conversations/${chatID}`).then(r => r.json())).data;
      if (latest.generation?.id !== generationID || latest.generation.status !== "running") {
        if (mounted.current) acceptServer(latest);
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 150));
    }
    throw new Error("停止尚未完成，请重试");
  }
  async function stopGeneration() {
    if (stopping.has(composerID)) return;
    const pending = pendingGenerations.current.get(composerID);
    if (pending) { pending.stopRequested = true; markStopping(composerID, true); return; }
    if(!selected?.generation?.id)return;
    markStopping(composerID, true);
    try { await stopChatGeneration(composerID, selected.generation.id); }
    catch(e){patchDraft(composerID, { error: (e as Error).message });}
    finally { if (mounted.current) markStopping(composerID, false); }
  }

  function newChat() {
    const nextID = id();
    setDraftID(nextID);
    patchDraft(nextID, { model });
    setCurrent("");
    setError("");
    setMenu(false);
  }
  function selectChat(chat: Chat) {
    setCurrent(chat.id);
    setError("");
    setMenu(false);
  }
  async function copy(item: Item) {
    try {
      await navigator.clipboard.writeText(item.content);
      setCopied(item.id);
      setTimeout(() => setCopied(""), 1800);
    } catch {
      setError("复制失败，请手动选择文本复制");
    }
  }
  return (
    <div className={`chat-shell ${historyCollapsed ? "history-collapsed" : ""}`}>
      <aside
        className={`history ${menu ? "is-open" : ""}`}
        aria-label="历史对话"
      >
        <div className="history-heading">
          <Button
            variant="ghost"
            size="icon"
            className="history-action"
            onClick={newChat}
            disabled={!historyReady}
            aria-label="新建会话"
            title="新建会话"
          >
            <Plus className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="history-action"
            onClick={() => {
              if (window.matchMedia("(max-width: 640px)").matches) setMenu(false);
              else setHistoryCollapsed(true);
            }}
            aria-label="收起会话列表"
            title="收起会话列表"
          >
            <PanelLeftClose className="size-4" />
          </Button>
        </div>
        <div className="history-list">
          {chats.length === 0 ? (
            <p className="history-empty">对话会自动保存到账号</p>
          ) : (
            chats.map((chat) => (
              <div
                key={chat.id}
                className={`history-row ${chat.id === current ? "active" : ""}`}
              >
                <button
                  onClick={() => selectChat(chat)}
                  disabled={!historyReady}
                  className="history-title"
                >
                  {preparing.has(chat.id) || chat.generation?.status === "running" ? <LoaderCircle className="size-4 shrink-0 animate-spin" aria-label="正在生成" /> : <MessageSquare className="size-4 shrink-0" />}
                  <span>{chat.title}</span>
                </button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="delete-chat"
                  disabled={!historyReady || preparing.has(chat.id) || chat.generation?.status === "running"}
                  title={preparing.has(chat.id) || chat.generation?.status === "running" ? "停止生成后可删除此对话" : "删除对话"}
                  aria-label={`删除对话：${chat.title}`}
                  onClick={() => {
                    if (window.confirm("删除这段对话？使用记录会保留。")) {
                      void deleteChat(chat.id);
                    }
                  }}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </div>
            ))
          )}
        </div>
      </aside>
      {menu && (
        <button
          className="history-backdrop"
          aria-label="关闭历史对话"
          onClick={() => setMenu(false)}
        />
      )}
      <main className="chat-main">
        {fastNotice && <div className="fast-toast" role="status">Fast 已开启，计费 ×3</div>}
        <Button
          variant="ghost"
          size="icon"
          className="history-toggle"
          aria-label="历史对话"
          onClick={() => setMenu(!menu)}
        >
          {historyCollapsed ? <PanelLeftOpen className="size-4" /> : <PanelLeft className="size-4" />}
        </Button>
        <Conversation className="conversation">
          <ConversationContent
            className={`conversation-content ${!messages.length ? "empty" : ""}`}
          >
            {detailLoading && !messages.length ? <div className="history-loading" role="status">正在加载对话…</div> : !messages.length ? (
              <ConversationEmptyState className="welcome-state">
                <div className="welcome-icon">
                  <Sparkles className="size-7" />
                </div>
                <h2>今天想聊些什么？</h2>
                <div className="welcome-suggestions" role="group" aria-label="试试这些问题">
                  {welcomeSuggestions.map(({ title, description, prompt, icon: Icon }) => (
                    <Suggestion
                      key={title}
                      className="welcome-suggestion"
                      aria-label={title}
                      suggestion={prompt}
                      disabled={!historyReady}
                      onClick={(text) => { setInput(text); textareaRef.current?.focus(); }}
                    >
                      <Icon className="size-4 welcome-suggestion-icon" aria-hidden="true" />
                      <span className="welcome-suggestion-copy">
                        <strong>{title}</strong>
                        <span>{description}</span>
                      </span>
                    </Suggestion>
                  ))}
                </div>
              </ConversationEmptyState>
            ) : (
              messages.map((item, index) => (
                <Message
                  from={item.role}
                  key={item.id}
                  className="chat-message"
                >
                  <MessageContent className={`message-content${item.role === "user" && item.images?.length ? " user-message-with-images" : ""}`}>
                    {item.role === "assistant" && (
                      <div className="model-label">
                        <BrandIcon
                          brand={modelProfile(item.model || "").brand}
                          color={modelProfile(item.model || "").color}
                        />
                        {item.model}
                        {item.effort && item.effort !== "default" && (
                          <span className="message-effort">
                            · {effortDetails[item.effort].label}
                          </span>
                        )}
                        {item.serviceTier === "fast" && <span className="message-effort">· Fast · 2 倍费用</span>}
                      </div>
                    )}
                    {item.role === "assistant" ? (
                      <MessageThinking reasoning={item.reasoning} tools={item.tools}
                        model={item.model} isStreaming={busy && index === messages.length - 1} />
                    ) : null}
                    {item.images?.length ? <ImageGallery images={item.images} /> : null}
                    {item.tools?.some(tool=>tool.name === "image_generation" && tool.status === "running") ? <ImageGenerationLoading /> : null}
                    {item.content && (item.role === "user" && item.images?.length ? (
                      <div className="user-message-text"><InlineToolTimeline text={item.content} tools={[]} offsetKey="contentOffset" /></div>
                    ) : <InlineToolTimeline isAnimating={busy && index === messages.length - 1} text={item.content} tools={(item.tools || []).filter(tool => tool.name !== "web_search" && tool.name !== "image_recognition" && (tool.contentOffset || 0) > 0)} offsetKey="contentOffset" />)}
                    {item.role === "assistant" && item.tools?.length ? <ImageGenerationModels tools={item.tools} /> : null}
                    {busy && index === messages.length - 1 && !item.content && !item.tools?.some(tool=>tool.name === "image_generation" && tool.status === "running") && (
                      <GenerationWait preparing={preparing.has(composerID)} connection={selected?.generation?.connection}
                        hasReasoning={!!item.reasoning}
                        tool={item.tools?.some(tool=>tool.name === "web_search" && tool.status === "running") ? "web_search" : item.tools?.some(tool=>tool.name === "image_recognition" && tool.status === "running") ? "image_recognition" : undefined}/>
                    )}
                    {item.error && (
                      <p className="message-error">{item.error}</p>
                    )}
                  </MessageContent>
                  {item.role === "assistant" && !busy && (
                    <MessageActions>
                      <MessageAction
                        label={copied === item.id ? "已复制" : "复制"}
                        onClick={() => copy(item)}
                        disabled={!item.content}
                      >
                        {copied === item.id ? (
                          <Check className="size-3.5" />
                        ) : (
                          <Copy className="size-3.5" />
                        )}
                      </MessageAction>
                      {index === messages.length - 1 && (
                        <MessageAction
                          label="重新生成"
                          onClick={() =>
                            void send("", true).catch(() => {})
                          }
                        >
                          <RotateCcw className="size-3.5" />
                        </MessageAction>
                      )}
                    </MessageActions>
                  )}
                </Message>
              ))
            )}
          </ConversationContent>
          <ConversationScrollButton />
        </Conversation>
        <div className="composer-area">
          {composerError && (
            <div className="error-banner" role="alert">
              <span>{composerError}</span>
              <button onClick={() => { patchDraft(composerID, { error: "" }); setError(""); setReload((n) => n + 1); void refresh(); }}>重试</button>
            </div>
          )}
          <PromptInput
            key={composerID}
            resetOnSubmit={false}
            submitDisabled={busy}
            disabled={!historyReady}
            attachments={{ files: draft?.files || [], setFiles }}
            accept="image/png,image/jpeg,image/webp"
            multiple
            maxFiles={uploadLimit}
            maxFileSize={5 * 1024 * 1024}
            onError={({ code }) => patchDraft(composerID, { error: code === "max_files" ? `本轮最多上传 ${uploadLimit} 张图片` : code === "max_file_size" ? "每张图片不能超过 5 MB" : "请选择 PNG、JPG 或 WebP 图片" })}
            onSubmit={(message: PromptInputMessage) => send(message.text, false, message.files)}
            className="composer"
          >
            <ImageAttachments disabled={!historyReady} maxFiles={uploadLimit} />
            <PromptInputTextarea
              ref={textareaRef}
              aria-label="消息"
              placeholder={busy ? "" : "输入消息或粘贴图片，Enter 发送，Shift + Enter 换行"}
              value={input}
              onChange={(e) => setInput(e.currentTarget.value)}
              disabled={!historyReady}
            />
            <PromptInputFooter>
              <PromptInputTools className="composer-tools">
                <ModelControls
                  model={model}
                  models={models}
                  effort={effort}
                  fast={fast}
                  disabled={!historyReady}
                  loading={loadingModels}
                  onModel={(value) => {
                    setModel(value);
                  }}
                  onEffort={(value) =>
                    setEfforts((all) => ({ ...all, [model]: value }))
                  }
                  onFast={(value) => { patchDraft(composerID, { fast: value }); setFastNotice(value); }}
                />
                <UploadImageButton disabled={!historyReady || loadingModels || !uploadLimit} maxFiles={uploadLimit} />
              </PromptInputTools>
              <ComposerSubmit busy={busy} stopping={stopping.has(composerID)} enabled={!!model && !loadingModels && historyReady && !detailLoading} input={input} stop={() => void stopGeneration()} />
            </PromptInputFooter>
          </PromptInput>
        </div>
      </main>
    </div>
  );
}

function ComposerSubmit({ busy, stopping, enabled, input, stop }: { busy: boolean; stopping: boolean; enabled: boolean; input: string; stop: () => void }) {
  const { files } = usePromptInputAttachments();
  return <PromptInputSubmit status={stopping ? "submitted" : busy ? "streaming" : "ready"}
    type={busy ? "button" : "submit"}
    disabled={stopping || (!busy && (!enabled || (!input.trim() && !files.length)))}
    onClick={busy ? (e) => { e.preventDefault(); stop(); } : undefined}
    aria-label={stopping ? "正在停止" : busy ? "停止生成" : "发送消息"} />;
}

createRoot(document.getElementById("root")!).render(<App />);
