import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import { flushSync } from "react-dom";
import { loadHistory, saveHistory } from "./history";
import { readGenerationStream } from "./generation-stream";
import { historyRequest } from "./history-api";
import { persistImages, persistRecognitionImages, supportsNativeImages, supplyGenerationImages } from "./images";
import type { Chat } from "./chat-types";

// Cached text appears first. The manifest contains no message bodies or image bytes.
export function useChatHistory(key: string, userID: number, preparing: ReadonlySet<string>, onError: (message: string) => void) {
  const [chats, setInternal] = useState<Chat[]>([]);
  const [current, setCurrent] = useState("");
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState("正在加载对话…");
  const [tick, setTick] = useState(0);
  const state = useRef(chats); state.current = chats;
  const preparingRef = useRef(preparing); preparingRef.current = preparing;
  const currentRef = useRef(current); currentRef.current = current;
  const alive = useRef(true);
  const etag = useRef("");
  const firstRefresh = useRef(true);
  const saving = useRef(new Set<string>());
  const foreground=useRef(new Set<string>());
  const saveRequests=useRef(new Map<string,AbortController>());
  const versions=useRef(new Map<string,number>());
  const loading = useRef(new Set<string>());
  const retryAfter = useRef(0);
  const cacheTimer=useRef<ReturnType<typeof setTimeout>|undefined>(undefined);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const validAccount = () => {
    try { return JSON.parse(localStorage.getItem("auth_user") || "null")?.id === userID && !!localStorage.getItem("auth_token"); } catch { return false; }
  };
  const setChats = useCallback((action: SetStateAction<Chat[]>) => {
    setInternal((all) => {
      const next = typeof action === "function" ? action(all) : action;
      return next.map((chat) => all.find((c) => c.id === chat.id) === chat ? chat : { ...chat, updated: Date.now(), dirty: true });
    });
  }, []);
  const refresh = useCallback(async () => {
    if (!validAccount()) return;
    try {
      const response = await historyRequest("/conversations", { headers: etag.current ? { "If-None-Match": etag.current } : {} });
      if (!alive.current || !validAccount()) return;
      if (response.status === 304) return;
      const remote: Chat[] = (await response.json()).data;
      etag.current = response.headers.get("ETag") || "";
      setInternal((cached) => {
        const merged = remote.map((meta) => {
          const local = cached.find((chat) => chat.id === meta.id);
          if (local?.dirty) return local;
          if (local && local.version === meta.version && local.loaded !== false) return local;
          return { ...meta, messages: local?.messages || [], loaded: false, dirty: false };
        });
        return [...cached.filter((chat) => chat.dirty && !remote.some((r) => r.id === chat.id)), ...merged].sort((a,b) => b.updated-a.updated);
      });
      if(firstRefresh.current){const remembered=localStorage.getItem(key+":current");if(remembered && remote.some(c=>c.id===remembered))setCurrent(remembered);else if(!currentRef.current && !state.current.length && remote.length)setCurrent(remote[0].id);}
      firstRefresh.current = false;
      retryAfter.current = 0;
      setStatus("已同步到云端");
    } catch (e) {
      if (alive.current) { setStatus("离线 · 已保留本地缓存"); onError((e as Error).message); }
    }
  }, [userID]);
  useEffect(() => {
    alive.current = true;
    loadHistory<Chat>(key).then((stored) => {
      if (!alive.current || !validAccount()) return;
      const valid = stored.filter((c) => typeof c.id === "string" && Array.isArray(c.messages));
      setInternal(valid.map((chat) => ({ ...chat, dirty: chat.dirty ?? !chat.version })));
      const last=localStorage.getItem(key+":current");setCurrent(valid.some(c=>c.id===last) ? last! : valid[0]?.id || "");
    }).catch(() => {}).finally(() => {
      if (alive.current) { setReady(true); void refresh(); }
    });
    const resume = () => { if (document.visibilityState === "visible") { retryAfter.current=0; void refresh(); setTick((n)=>n+1); } };
    window.addEventListener("online", resume); document.addEventListener("visibilitychange", resume);
    const interval = setInterval(resume, 120000);
    return () => { alive.current = false; clearInterval(interval); clearTimeout(saveTimer.current);clearTimeout(cacheTimer.current); window.removeEventListener("online", resume); document.removeEventListener("visibilitychange", resume); };
  }, [key, refresh]);
  useEffect(() => {
    if(!ready || !validAccount() || cacheTimer.current)return;
    cacheTimer.current=setTimeout(()=>{cacheTimer.current=undefined;void saveHistory(key,state.current).catch(()=>onError("本地缓存空间不足，对话将继续保存在服务器"));},400);
  },[chats,ready,key]);
  useEffect(()=>{if(ready)localStorage.setItem(key+":current",current);},[current,ready,key]);
  const acceptServer = useCallback((chat: Chat) => {
    versions.current.set(chat.id,chat.version || 0);
    setInternal((all)=>all.some(c=>c.id===chat.id) ? all.map(c=>c.id===chat.id ? {...chat,messages:chat.messages || [],loaded:true,dirty:false} : c) : [{...chat,loaded:true,dirty:false},...all]);
  },[]);
  const selected = chats.find((chat) => chat.id === current);
  useEffect(() => {
    if (!selected || selected.loaded !== false || loading.current.has(selected.id)) return;
    const chatID = selected.id;
    loading.current.add(chatID);
    void historyRequest(`/conversations/${chatID}`).then((r)=>r.json()).then(({data}) => {
      if (!alive.current || !validAccount()) return;
      setInternal((all)=>all.map((c)=>c.id === chatID && !c.dirty ? {...data,messages:data.messages || [],loaded:true,dirty:false} : c));
    }).catch((e)=>{if(alive.current)onError(e.message);}).finally(()=>loading.current.delete(chatID));
  }, [current,selected?.version,selected?.loaded,tick]);
  useEffect(() => {
    if (!ready || saveTimer.current || !chats.some((c)=>c.dirty && !c.conflicted) || Date.now()<retryAfter.current) return;
    saveTimer.current = setTimeout(async () => {
      saveTimer.current=undefined;
      if (!validAccount()) return;
      for (const snapshot of state.current.filter((c)=>c.dirty && !c.conflicted && c.loaded !== false && c.generation?.status !== "running" && !preparingRef.current.has(c.id) && !foreground.current.has(c.id))) {
        if (preparingRef.current.has(snapshot.id) || foreground.current.has(snapshot.id)) continue;
        if (saving.current.has(snapshot.id)) continue;
        saving.current.add(snapshot.id);
        const controller=new AbortController();
        saveRequests.current.set(snapshot.id,controller);
        setStatus("正在保存…");
        try {
          const messages = supportsNativeImages(snapshot.model)
            ? await persistImages(snapshot.messages)
            : await persistRecognitionImages(snapshot.messages, true);
          if(controller.signal.aborted || foreground.current.has(snapshot.id))continue;
          const payload = {...snapshot, version:snapshot.version || 0, messages:messages.map((m)=>({...m,images:m.images?.map(({url,...image})=>image)}))};
          const response = await historyRequest(`/conversations/${snapshot.id}`,{method:"PUT",body:JSON.stringify(payload),signal:controller.signal});
          const {data} = await response.json();
          if(controller.signal.aborted || foreground.current.has(snapshot.id))continue;
          versions.current.set(snapshot.id,Math.max(versions.current.get(snapshot.id) || 0,data.version));
          if (!alive.current || !validAccount()) return;
          setInternal((all)=>all.map((c)=> c.id === snapshot.id ? {
            ...c, version:data.version, dirty:c.updated !== snapshot.updated,
            messages:c.updated===snapshot.updated ? messages : c.messages,
          } : c));
          setStatus("已同步到云端");
        } catch (e) {
          if(controller.signal.aborted || foreground.current.has(snapshot.id))continue;
          retryAfter.current=Date.now()+30000;
          if ((e as {status?:number}).status===409 && alive.current) {
            const forkID=crypto.randomUUID();
            setInternal(all=>all.map(c=>c.id===snapshot.id ? {...c,id:forkID,title:c.title.slice(0,35)+"（未同步副本）",version:0,generation:undefined,dirty:true,conflicted:false,updated:Date.now()} : c));
            if(currentRef.current===snapshot.id)setCurrent(forkID);
            etag.current="";void refresh();
            onError("对话已在其他页面更新，未同步内容已保留为新对话");
          } else if (alive.current) { setStatus("同步失败 · 本地已保留");onError((e as Error).message); }
        } finally { saving.current.delete(snapshot.id); saveRequests.current.delete(snapshot.id); }
      }
    }, 400);
  }, [chats,ready,preparing,tick]);
  const runningKey = chats.filter(c => c.generation?.status === "running").map(c => `${c.id}:${c.generation!.id}`).sort().join(",");
  useEffect(()=>{
    // Each conversation owns its subscription. Navigation never cancels a reply.
    const cleanups = state.current.filter(c => c.generation?.status === "running").map(chat => {
    let active=true;let timer:ReturnType<typeof setTimeout>;let controller:AbortController;
    const imageDeliveries=new Map<string,Promise<void>>();
    let content=chat.messages.find(m=>m.id===chat.generation!.id)?.content || "";
    let reasoning=chat.messages.find(m=>m.id===chat.generation!.id)?.reasoning || "";
    let contentOffset=Array.from(content).length;let reasoningOffset=Array.from(reasoning).length;let retries=0;
    const connection=(value: "connecting" | "connected" | "reconnecting")=>{
      if(!active)return;
      setInternal(all=>all.map(c=>c.id===chat.id && c.generation?.id===chat.generation!.id && c.generation.connection!==value ? {...c,generation:{...c.generation,connection:value}} : c));
    };
    const connect=async()=>{
      controller=new AbortController();let finished=false;
      connection(retries ? "reconnecting" : "connecting");
      try {
        // A fetch can stall before headers, before the stream reader's idle
        // timer exists. Reconnect only the reader; the durable job keeps running.
        const headerTimer=setTimeout(()=>controller.abort(),15000);
        let response:Response;
        try {
          response=await historyRequest(`/generations/${chat.generation!.id}/events?content_offset=${contentOffset}&reasoning_offset=${reasoningOffset}`,{signal:controller.signal});
        } finally {clearTimeout(headerTimer);}
        connection("connected");
        await readGenerationStream(response,data=>{
          if(!active)return true;
          if(data.imageRequest?.id && validAccount() && !imageDeliveries.has(data.imageRequest.id)) {
            const requestID=data.imageRequest.id;
            const task=(async()=>{
              let messages=state.current.find(c=>c.id===chat.id)?.messages || [];
              const known=new Set(messages.flatMap(message=>message.images || []).map(image=>image.clientID));
              if(data.imageRequest.imageIDs.some((id:string)=>!known.has(id))) {
                const canonical=await historyRequest(`/conversations/${chat.id}`,{signal:controller.signal}).then(response=>response.json());
                messages=canonical.data.messages || [];
              }
              if(validAccount())await supplyGenerationImages(chat.generation!.id,data.imageRequest,messages,controller.signal);
            })().catch(()=>{imageDeliveries.delete(requestID);});
            imageDeliveries.set(requestID,task);
          }
          retries=0;
          const firstVisible=currentRef.current===chat.id && ((!reasoning && !!data.reasoning) || (!content && !!data.content));
          content+=data.content || "";reasoning+=data.reasoning || "";
          contentOffset+=Array.from(data.content || "").length;reasoningOffset+=Array.from(data.reasoning || "").length;
          // Terminal frames clear the transient server buffers. Keep both the
          // visible tools and this subscription until the canonical transcript
          // arrives: changing runningKey here would cancel that final fetch.
          const apply=()=>setInternal(all=>all.map(c=>c.id===chat.id && c.generation?.id===data.generation.id ? {...c,generation:data.generation.status === "running" ? {...data.generation,connection:"connected"} : c.generation,messages:c.messages.map(m=>m.id===data.generation.id?{...m,content,reasoning,tools:data.generation.status === "running" ? data.tools ?? m.tools : m.tools}:m)}:c));
          // Commit only the first real thought/text immediately. Later tokens
          // keep normal batching, and background chats never interrupt typing.
          if(firstVisible)flushSync(apply);else apply();
          if(data.generation.status !== "running"){finished=true;return true;}
          return false;
        });
        if(finished && active){
          const final=await historyRequest(`/conversations/${chat.id}`).then(r=>r.json());
          if(active)acceptServer(final.data);
          window.parent.postMessage({type:"chat:usage-updated"},location.origin);return;
        }
      } catch(e) {if(active){connection("reconnecting");setStatus("正在重新连接回复…");}}
      if(active)timer=setTimeout(connect,Math.min(15000,1000*2**retries++));
    };
    void connect();
    return()=>{active=false;clearTimeout(timer);controller?.abort();};
    });
    return () => cleanups.forEach(cleanup => cleanup());
  },[runningKey,acceptServer]);
  const beginSend=(chatID:string)=>{
    // A user turn has priority over background history and image-cache work.
    foreground.current.add(chatID);
    saveRequests.current.get(chatID)?.abort();
    return Math.max(versions.current.get(chatID) || 0,state.current.find(c=>c.id===chatID)?.version || 0);
  };
  const finishSend=(chatID:string)=>{foreground.current.delete(chatID);};
  const deleteChat = async (chatID: string) => {
    // An in-flight save finishes first; the tombstone rejects all later stale saves.
    if (saving.current.has(chatID)) { onError("对话正在保存，请稍后删除"); return; }
    try {
      if (state.current.find((c)=>c.id===chatID)?.version) await historyRequest(`/conversations/${chatID}`,{method:"DELETE"});
      setInternal((all)=>all.filter((c)=>c.id!==chatID));
      if (currentRef.current===chatID) setCurrent("");
    } catch(e) {onError((e as Error).message);}
  };
  return {chats,setChats,current,setCurrent,ready,status,deleteChat,refresh,detailLoading:selected?.loaded===false,acceptServer,beginSend,finishSend};
}
