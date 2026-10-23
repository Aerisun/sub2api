const historyBase = () => `${location.pathname.split("/chat-ui/")[0]}/api/v1/user/chat`;
let owner: string | undefined;
function ownerID(){try{return String(JSON.parse(localStorage.getItem("auth_user") || "null")?.id);}catch{return "";}}
export async function historyRequest(path: string, init: RequestInit = {}) {
  owner ??= ownerID();
  if(owner !== ownerID())throw new Error("登录状态已改变，请刷新页面");
  const response = await fetch(historyBase() + path, { ...init, signal:init.signal || AbortSignal.timeout(path === "/images" ? 120000 : 30000), headers: {
    "Content-Type": "application/json", Authorization: `Bearer ${localStorage.getItem("auth_token") || ""}`,
    ...init.headers,
  }});
  if(owner !== ownerID())throw new Error("登录状态已改变，请刷新页面");
  if (!response.ok && response.status !== 304) {
    const body = await response.json().catch(() => ({}));
    throw Object.assign(new Error(body.message || "对话同步失败，请检查网络后重试"), { status: response.status });
  }
  return response;
}
