// IndexedDB retains image messages without localStorage's small string quota.
let database: Promise<IDBDatabase> | undefined;
function openDatabase() {
  return database ??= new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open("aerisun-chat", 2);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains("history")) request.result.createObjectStore("history");
      if (!request.result.objectStoreNames.contains("images")) request.result.createObjectStore("images");
    };
    request.onsuccess = () => {request.result.onversionchange=()=>request.result.close();resolve(request.result);};
    request.onerror = () => { database = undefined; reject(request.error); };
  });
}
export async function loadHistory<T>(key: string): Promise<T[]> {
  const db = await openDatabase();
  const stored = await new Promise<unknown>((resolve, reject) => {
    const request = db.transaction("history").objectStore("history").get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  if (Array.isArray(stored)) return stored;
  if (stored && typeof stored === "object" && "format" in stored && stored.format===2 && "ids" in stored && Array.isArray(stored.ids)) {
    const tx=db.transaction("history");
    const rows=await Promise.all(stored.ids.map((id)=>new Promise<T|undefined>((resolve,reject)=>{const req=tx.objectStore("history").get(`${key}:conversation:${id}`);req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);})));return rows.filter(row=>row!==undefined) as T[];
  }
  const legacy = JSON.parse(localStorage.getItem(key) || "[]");
  return Array.isArray(legacy) ? legacy : [];
}
const savedObjects=new Map<string,Map<string,unknown>>();
export async function saveHistory(key: string, chats: unknown[]) {
  const db=await openDatabase();
  const previous=savedObjects.get(key) || new Map<string,unknown>();
  const next=new Map<string,unknown>();
  const items=chats as {id:string;updated:number;messages:unknown[];dirty?:boolean;loaded?:boolean}[];
  const recent=new Set(items.slice().sort((a,b)=>b.updated-a.updated).slice(0,20).map(c=>c.id));
  const current=localStorage.getItem(key+":current");
  await new Promise<void>((resolve,reject)=>{
    const tx=db.transaction("history","readwrite");const store=tx.objectStore("history");
    for(const chat of items){next.set(chat.id,chat);if(previous.get(chat.id)===chat)continue;
      const cached=recent.has(chat.id)||chat.id===current||chat.dirty ? chat : {...chat,messages:[],loaded:false};
      store.put(cached,`${key}:conversation:${chat.id}`);
    }
    for(const id of previous.keys())if(!next.has(id))store.delete(`${key}:conversation:${id}`);
    store.put({format:2,ids:items.map(c=>c.id)},key);
    tx.oncomplete=()=>resolve();tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
  });
  savedObjects.set(key,next);localStorage.removeItem(key);
}
export async function cachedImage(key: string): Promise<string | undefined> {
  const db = await openDatabase();
  return new Promise((resolve, reject) => { const request=db.transaction("images").objectStore("images").get(key); request.onsuccess=()=>resolve(typeof request.result === "string" ? request.result : request.result?.url);request.onerror=()=>reject(request.error); });
}
export async function cacheImage(key: string, url: string) {
  const db=await openDatabase();
  await new Promise<void>((resolve,reject)=>{const tx=db.transaction("images","readwrite");tx.objectStore("images").put({url,used:Date.now(),bytes:url.length},key);tx.oncomplete=()=>{scheduleImageTrim(key);resolve();};tx.onerror=()=>reject(tx.error);});
}

// Bound each account's browser cache. Recognition originals exist only here;
// after eviction the user must upload them again to retry recognition.
let trimTimer: ReturnType<typeof setTimeout> | undefined;
function scheduleImageTrim(key:string) {
  if(trimTimer)return;
  trimTimer=setTimeout(async()=>{
    trimTimer=undefined;
    try{
      const db=await openDatabase();const prefix=key.split(":")[0]+":";
      const rows:{key:IDBValidKey;used:number;bytes:number}[]=[];
      await new Promise<void>((resolve,reject)=>{const request=db.transaction("images").objectStore("images").openCursor(IDBKeyRange.bound(prefix,prefix+"\uffff"));request.onsuccess=()=>{const cursor=request.result;if(!cursor){resolve();return};const v=cursor.value;rows.push({key:cursor.key,used:v.used || 0,bytes:v.bytes || (typeof v==="string"?v.length:0)});cursor.continue();};request.onerror=()=>reject(request.error);});
      let bytes=rows.reduce((total,row)=>total+row.bytes,0);let count=rows.length;
      const tx=db.transaction("images","readwrite");for(const row of rows.sort((a,b)=>a.used-b.used)){if(bytes<=64*1024*1024 && count<=200)break;tx.objectStore("images").delete(row.key);bytes-=row.bytes;count--;}
    }catch{}
  },2000);
}
