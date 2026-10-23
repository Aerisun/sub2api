import catalog from "../../frontend/src/config/aerisunModels.json" with { type: "json" };
import { cachedImage, cacheImage } from "./history.ts";
import { historyRequest } from "./history-api.ts";
import type { Item } from "./chat-types";
export type ChatImage = { url?: string; assetID?: string; clientID?: string; name: string; mediaType: string };
export const MAX_RECOGNITION_IMAGES = 12;
export function imageKey(image: ChatImage): string { return image.clientID ? `browser:${image.clientID}` : image.assetID || ""; }
export type HistoryImageChoice = { key: string; image: ChatImage; recognized: boolean; messageID: string };
export function historyImageChoices(messages: Item[]): HistoryImageChoice[] {
  const recognized = new Set(messages.flatMap(message => (message.tools || [])
    .filter(tool => tool.name === "image_recognition" && tool.status === "completed" && tool.output?.trim())
    .flatMap(tool => (tool.input?.images || []).map(imageKey))));
  const unique = new Map<string,HistoryImageChoice>();
  for (const message of messages) if (message.role === "user") {
    for (const image of message.images || []) {
      const key = imageKey(image);
      if (key && !unique.has(key)) unique.set(key,{key,image,recognized:recognized.has(key),messageID:message.id});
    }
  }
  return [...unique.values()];
}

const imageLoads = new Map<string, Promise<string>>();
const uploads = new Map<string, Promise<string>>();
function account() { return JSON.parse(localStorage.getItem("auth_user") || "null")?.id; }
export async function imageURL(image: ChatImage): Promise<string> {
  if (image.url) return image.url;
  if (image.clientID) {
    const cached = await cachedImage(`${account()}:local:${image.clientID}`);
    if (!cached) throw new Error("图片仅保存在上传时的浏览器，缓存已清理或设备已更换，请重新上传");
    return cached;
  }
  if (!image.assetID) throw new Error("图片不存在，请重新上传");
  const key = `${account()}:${image.assetID}`;
  const existing = imageLoads.get(key); if (existing) return existing;
  const pending = (async () => {
    const cached = await cachedImage(key).catch(()=>undefined);if(cached)return cached;
    const response=await historyRequest(`/images/${image.assetID}`);
    const blob=await response.blob();
    const url=await new Promise<string>((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result as string);r.onerror=()=>reject(r.error);r.readAsDataURL(blob);});
    void cacheImage(key,url).catch(()=>{});return url;
  })();
  imageLoads.set(key,pending);pending.finally(()=>imageLoads.delete(key)).catch(()=>{});return pending;
}
export async function resolveImages(messages: Item[]): Promise<Item[]> {
  return Promise.all(messages.map(async(m)=>({...m,images:m.images ? await Promise.all(m.images.map(async(im)=>({...im,url:await imageURL(im)}))) : undefined})));
}
export async function persistImages(messages: Item[]): Promise<Item[]> {
  return Promise.all(messages.map(async(m)=>({...m,images:m.images ? await Promise.all(m.images.map(async(image)=>{
    if(image.assetID || image.clientID)return image;
    const { clientID: _clientID, ...serverImage } = image;
    const url=await imageURL(image);const imageOwner=account();
    const raw=await fetch(url).then(r=>r.arrayBuffer());
    const digest=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",raw)),v=>v.toString(16).padStart(2,"0")).join("");
    const digestKey=`${imageOwner}:sha256:${digest}`;
    const mapping=await cachedImage(digestKey).catch(()=>undefined);
    if(mapping){try{const entry=JSON.parse(mapping);if(entry.expires>Date.now()){void cacheImage(`${imageOwner}:${entry.assetID}`,url).catch(()=>{});return {...serverImage,assetID:entry.assetID,url:undefined};}}catch{}}
    const key=`${imageOwner}:${digest}`;
    let upload=uploads.get(key);
    if(!upload){upload=historyRequest("/images",{method:"POST",body:JSON.stringify({url})}).then(r=>r.json()).then(body=>body.data.assetID as string);uploads.set(key,upload);upload.finally(()=>uploads.delete(key)).catch(()=>{});}
    const assetID=await upload;void cacheImage(`${imageOwner}:${assetID}`,url).catch(()=>{});void cacheImage(digestKey,JSON.stringify({assetID,expires:Date.now()+12*60*60*1000})).catch(()=>{});return {...serverImage,assetID,url:undefined};
  })) : undefined})));
}

// Recognition originals stay in this browser. Only IDs/names enter the cloud
// transcript; the data URL travels separately in the generation request.
export async function persistRecognitionImages(messages: Item[], preserveSaved = false): Promise<Item[]> {
  return Promise.all(messages.map(async message => ({ ...message, images: message.images
    ? await Promise.all(message.images.map(async image => {
      if (preserveSaved && (image.clientID || image.assetID)) return image;
      const url = await imageURL(image);
      const clientID = image.clientID || crypto.randomUUID();
      await cacheImage(`${account()}:local:${clientID}`, url);
      return { clientID, name:image.name, mediaType:image.mediaType };
    })) : undefined })));
}

export async function recognitionImageInputs(messages: Item[]): Promise<Record<string,string>> {
  const current = messages.slice().reverse().find(message=>message.role === "user");
  return Object.fromEntries(await Promise.all((current?.images || []).filter(image=>image.clientID)
    .map(async image => [image.clientID!, await imageURL(image)])));
}

export type GenerationImageRequest = { id: string; imageIDs: string[] };
export async function supplyGenerationImages(generationID: string, request: GenerationImageRequest, messages: Item[], signal: AbortSignal): Promise<void> {
  if (!request.id || !Array.isArray(request.imageIDs) || request.imageIDs.length > MAX_RECOGNITION_IMAGES || new Set(request.imageIDs).size !== request.imageIDs.length) throw new Error("图片读取请求无效");
  const known = new Map(historyImageChoices(messages).filter(choice=>choice.image.clientID).map(choice=>[choice.image.clientID!,choice.image]));
  const resolved = await Promise.all(request.imageIDs.map(async id=>{
    const image=known.get(id);
    if(!image)return {id};
    try {return {id,url:await imageURL(image)};} catch {return {id};}
  }));
  if (signal.aborted) throw new DOMException("已取消","AbortError");
  const images=Object.fromEntries(resolved.filter(entry=>entry.url).map(entry=>[entry.id,entry.url]));
  const unavailable=resolved.filter(entry=>!entry.url).map(entry=>entry.id);
  await historyRequest(`/generations/${encodeURIComponent(generationID)}/image-inputs`,{method:"POST",signal,body:JSON.stringify({requestID:request.id,images,unavailable})});
}

export function supportsImages(model: string): boolean {
  const name = model.toLowerCase().split("/").at(-1) || "";
  const entry = Object.entries(catalog).find(([id]) => id.toLowerCase() === name)?.[1];
  return entry && "modalities" in entry ? entry.modalities?.input?.includes("image") ?? false : false;
}
export function supportsNativeImages(model: string): boolean {
  const name = model.toLowerCase().split("/").at(-1) || "";
  return supportsImages(model) && !["glm-5.2", "glm-5.3", "nemotron-3.5"].includes(name);
}
export function toChatMessages(items: { role: "user" | "assistant"; content: string; images?: ChatImage[]; reasoning?: string; error?: string }[]) {
  let latestUser = -1;
  items.forEach((item,index)=>{if(item.role === "user")latestUser=index;});
  return items.map((item,index)=>index<latestUser && item.images?.length ? {...item,content:item.content+"\n[历史图片已在前文处理，本轮不重新识别。]",images:undefined} : item).filter((m) => (m.content || m.images?.length) && !m.error).map((m) => ({
    role: m.role,
    content: m.role === "user" && m.images?.length
      ? [ ...(m.content ? [{ type: "text", text: m.content }] : []),
          ...m.images.map((image) => ({ type: "image_url", image_url: { url: image.url } })) ]
      : m.content,
    ...(m.role === "assistant" && m.reasoning ? { reasoning_content: m.reasoning } : {}),
  }));
}
