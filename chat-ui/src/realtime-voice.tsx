import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AudioLines, Mic, PhoneOff, X } from "lucide-react";
import captureURL from "./realtime-capture.js?url&no-inline";
import dialogStyles from "./realtime-dialog.css?url&no-inline";

const preferredModel = "gpt-realtime-2.1-mini";
const phases = {
  idle: "通话已结束", permission: "等待麦克风授权", connecting: "正在连接",
  listening: "请说话", recording: "正在听你说", thinking: "正在回应", speaking: "正在播放",
};
type Phase = keyof typeof phases;
type MicFrame = { pcm: ArrayBuffer; rms: number };
type VoiceEvent = {
  type: string;
  delta?: string;
  transcript?: string;
  session?: { model?: string };
  response?: { status?: string };
  error?: { type?: string; code?: string };
};

class VoiceSession {
  private closed = false;
  private socket?: WebSocket;
  private stream?: MediaStream;
  private source?: MediaStreamAudioSourceNode;
  private capture?: AudioWorkletNode;
  private timeout?: number;
  private sources = new Set<AudioBufferSourceNode>();
  private playhead = 0;
  private phase: Phase = "permission";
  private preRoll: ArrayBuffer[] = [];
  private loudFrames = 0;
  private quietFrames = 0;
  private recordingFrames = 0;
  private captions = "";

  constructor(
    private audio: AudioContext,
    private model: string,
    private onPhase: (phase: Phase) => void,
    private onCaption: (caption: string) => void,
    private onError: (error: string) => void,
  ) {}

  async start(apiBase: string, token: string) {
    try {
      const playback = this.audio.resume();
      const mic = navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      }).then(stream => {
        if (this.closed) stream.getTracks().forEach(track => track.stop());
        else this.stream = stream;
        return stream;
      });
      const [stream] = await Promise.all([mic, playback, this.audio.audioWorklet.addModule(captureURL)]);
      if (this.closed) return;
      this.source = this.audio.createMediaStreamSource(stream);
      this.capture = new AudioWorkletNode(this.audio, "realtime-capture", { numberOfOutputs: 0 });
      this.capture.port.onmessage = ({ data }: MessageEvent<MicFrame>) => this.handleMic(data);
      this.source.connect(this.capture);
      stream.getAudioTracks()[0].addEventListener("ended", () => this.fail("麦克风已断开，请重新连接。"));
      const endpoint = new URL(apiBase + "/chat/realtime", location.origin);
      endpoint.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      endpoint.searchParams.set("model", this.model);
      const socket = this.socket = new WebSocket(endpoint, ["sub2api-voice", "jwt." + token]);
      this.setPhase("connecting");
      this.setTimeout(() => this.fail("语音连接超时，请重试。"), 40000);
      socket.onmessage = ({ data }) => {
        try { this.handleEvent(JSON.parse(data)); }
        catch { this.fail("语音数据处理失败，请重试。"); }
      };
      socket.onerror = () => this.fail("语音连接失败，请检查登录状态、余额和网络后重试。");
      socket.onclose = ({ wasClean }) => {
        if (wasClean) this.close();
        else this.fail("语音连接已断开，请重新开始通话。");
      };
    } catch (error) {
      this.fail(error instanceof DOMException && error.name === "NotAllowedError"
        ? "请在浏览器里允许麦克风和音频播放，再开始通话。"
        : "无法启动麦克风，请确认浏览器和设备支持实时音频。");
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    window.clearTimeout(this.timeout);
    this.stream?.getTracks().forEach(track => track.stop());
    this.source?.disconnect();
    if (this.capture) {
      this.capture.port.onmessage = null;
      this.capture.port.close();
    }
    for (const source of this.sources) source.stop();
    this.sources.clear();
    this.socket?.close();
    void this.audio.close().catch(() => {});
    this.setPhase("idle");
  }

  private fail(message: string) {
    if (this.closed) return;
    this.onError(message);
    this.close();
  }

  private setPhase(phase: Phase) {
    this.phase = phase;
    this.onPhase(phase);
  }

  private setTimeout(callback: () => void, ms: number) {
    window.clearTimeout(this.timeout);
    this.timeout = window.setTimeout(callback, ms);
  }

  private send(type: string, fields: Record<string, unknown> = {}) {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    if (this.socket.bufferedAmount > 256 * 1024) {
      this.fail("网络发送过慢，语音通话已结束。");
      return;
    }
    this.socket.send(JSON.stringify({ type, ...fields }));
  }

  private requestReply() {
    this.captions = "";
    this.setPhase("thinking");
    this.setTimeout(() => this.fail("等待回复超时，请重新开始通话。"), 50000);
    this.send("response.create", { response: { output_modalities: ["audio"], max_output_tokens: 256 } });
  }

  private handleEvent(event: VoiceEvent) {
    if (this.closed) return;
    switch (event.type) {
      case "session.created":
        if (event.session?.model !== this.model) {
          this.fail("语音型号校验失败，请重试。");
          break;
        }
        this.send("session.update", {
          session: {
            type: "realtime",
            instructions: "你是本站的语音对话助手。首次回复用一句自然、简短的中文打招呼并邀请对方说话。之后用用户说话的语言回答，口语自然，回答简洁。",
            audio: { input: { turn_detection: null } },
          },
        });
        break;
      case "session.updated":
        if (this.phase !== "connecting") break;
        this.send("conversation.item.create", {
          item: { type: "message", role: "user", content: [{ type: "input_text", text: "开始语音通话，请先打招呼。" }] },
        });
        this.requestReply();
        break;
      case "response.output_audio.delta":
        if (event.delta) this.play(event.delta);
        break;
      case "response.output_audio_transcript.delta":
        this.captions += event.delta || "";
        this.onCaption(this.captions);
        break;
      case "response.output_audio_transcript.done":
        if (event.transcript) this.onCaption(event.transcript);
        break;
      case "response.done":
        if (event.response?.status !== "completed") {
          this.fail("语音回复提前结束，请重新开始通话。");
          break;
        }
        window.parent.postMessage({ type: "chat:usage-updated" }, location.origin);
        // Microphone upload remains paused until the browser has played the reply.
        this.setTimeout(() => this.setPhase("listening"),
          Math.max(0, this.playhead - this.audio.currentTime) * 1000 + 160);
        break;
      case "error":
        this.fail(event.error?.type === "insufficient_quota"
          ? "余额或额度不足，语音通话已结束。"
          : "语音服务暂时不可用，请重新连接。");
        break;
    }
  }

  private play(delta: string) {
    const bytes = Uint8Array.from(atob(delta), value => value.charCodeAt(0));
    const pcm = new Int16Array(bytes.buffer);
    if (!pcm.length) return;
    const buffer = this.audio.createBuffer(1, pcm.length, 24000);
    buffer.getChannelData(0).set(Float32Array.from(pcm, value => value / 32768));
    const source = this.audio.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audio.destination);
    this.sources.add(source);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    this.playhead = Math.max(this.playhead, this.audio.currentTime + 0.035);
    source.start(this.playhead);
    this.playhead += buffer.duration;
    if (this.phase !== "speaking") this.setPhase("speaking");
  }

  private handleMic({ pcm, rms }: MicFrame) {
    if (this.closed || (this.phase !== "listening" && this.phase !== "recording")) return;
    const loud = rms >= 0.016;
    if (this.phase === "listening") {
      this.preRoll.push(pcm);
      if (this.preRoll.length > 3) this.preRoll.shift();
      this.loudFrames = loud ? this.loudFrames + 1 : 0;
      if (this.loudFrames < 2) return;
      this.setPhase("recording");
      this.recordingFrames = this.preRoll.length;
      this.quietFrames = 0;
      for (const frame of this.preRoll) this.append(frame);
      this.preRoll = [];
      return;
    }

    this.append(pcm);
    this.recordingFrames++;
    this.quietFrames = loud ? 0 : this.quietFrames + 1;
    if (this.quietFrames < 9 && this.recordingFrames < 300) return;
    this.loudFrames = 0;
    this.send("input_audio_buffer.commit");
    if (!this.closed) this.requestReply();
  }

  private append(pcm: ArrayBuffer) {
    const bytes = new Uint8Array(pcm);
    this.send("input_audio_buffer.append", { audio: btoa(String.fromCharCode(...bytes)) });
  }
}

export function RealtimeVoice({ apiBase, isSignedIn }: { apiBase: string; isSignedIn: () => boolean }) {
  const button = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const session = useRef<VoiceSession | null>(null);
  const [modalRoot] = useState(() => document.createElement("div"));
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [caption, setCaption] = useState("");

  useEffect(() => {
    const container = window.frameElement?.parentElement ?? document.body;
    const mount = () => container.append(modalRoot);
    const detach = () => {
      dialog.current?.close();
      modalRoot.remove();
    };
    mount();
    window.addEventListener("pageshow", mount);
    window.addEventListener("pagehide", detach);
    return () => {
      window.removeEventListener("pageshow", mount);
      window.removeEventListener("pagehide", detach);
      detach();
    };
  }, [modalRoot]);

  useEffect(() => {
    const token = localStorage.getItem("auth_token");
    if (!token || !isSignedIn()) return;
    const controller = new AbortController();
    fetch(apiBase + "/chat/realtime/models", {
      headers: { Authorization: "Bearer " + token }, signal: controller.signal,
    }).then(async response => {
      if (!response.ok) return;
      const available: string[] = (await response.json()).models ?? [];
      if (controller.signal.aborted || !isSignedIn()) return;
      setModels(available);
      setModel(available.includes(preferredModel) ? preferredModel : available[0] || "");
    }).catch(() => {});
    const handleStorage = () => {
      if (!isSignedIn()) {
        controller.abort();
        session.current?.close();
        setModels([]);
      }
    };
    const close = () => session.current?.close();
    window.addEventListener("storage", handleStorage);
    window.addEventListener("pagehide", close);
    return () => {
      controller.abort();
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener("pagehide", close);
      close();
    };
  }, [apiBase, isSignedIn]);

  function start() {
    const token = localStorage.getItem("auth_token");
    if (!model || !token || !isSignedIn()) return;
    setError("");
    setCaption("");
    if (!window.isSecureContext || !window.AudioContext || !navigator.mediaDevices?.getUserMedia) {
      setError("当前浏览器无法使用实时麦克风。请用支持麦克风的 HTTPS 浏览器打开。");
      return;
    }
    session.current?.close();
    try {
      const audio = new AudioContext();
      const current = new VoiceSession(audio, model, setPhase, setCaption, setError);
      session.current = current;
      setPhase("permission");
      void current.start(apiBase, token);
    } catch {
      setError("浏览器无法启动音频，请重试。");
    }
  }

  function dismiss() {
    session.current?.close();
    dialog.current?.close();
    button.current?.focus();
  }

  return (
    <>
      {models.length > 0 && (
        <button ref={button} type="button" className="realtime-trigger" aria-label="实时语音" aria-haspopup="dialog" onClick={() => {
          dialog.current?.showModal();
          start();
        }}>
          <AudioLines size={16} aria-hidden="true" />
        </button>
      )}
      {createPortal(<>
        <link rel="stylesheet" href={dialogStyles} />
        <dialog ref={dialog} className="realtime-dialog" aria-labelledby="voice-title" onCancel={event => {
          event.preventDefault();
          dismiss();
        }}>
          <div className="realtime-panel">
            <header>
              <h2 id="voice-title">实时语音</h2>
              <button type="button" className="realtime-close" onClick={dismiss} aria-label="关闭实时语音">
                <X size={18} aria-hidden="true" />
              </button>
            </header>
            <div className="realtime-voice-status" data-phase={phase}>
              <Mic size={26} aria-hidden="true" />
              <p role="status">{phases[phase]}</p>
            </div>
            {error && <p role="alert" className="realtime-error">{error}</p>}
            {caption && <div className="realtime-caption">{caption}</div>}
            <footer>
              <select value={model} onChange={event => setModel(event.currentTarget.value)} disabled={phase !== "idle"} aria-label="语音模型">
                {models.map(name => <option value={name} key={name}>{name}</option>)}
              </select>
              {phase === "idle" ? (
                <button type="button" className="realtime-action" onClick={start}>开始通话</button>
              ) : (
                <button type="button" className="realtime-action realtime-stop" onClick={() => session.current?.close()}>
                  <PhoneOff size={15} aria-hidden="true" />结束通话
                </button>
              )}
            </footer>
          </div>
        </dialog>
      </>, modalRoot)}
    </>
  );
}
