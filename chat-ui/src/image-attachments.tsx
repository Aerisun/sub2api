import { ImageGenerationLoading } from "./image-generation-loading";
import { useEffect, useState } from "react";
import { imageURL } from "./images";
import { Download, ImagePlus, LoaderCircle, X } from "lucide-react";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { usePromptInputAttachments } from "@/components/ai-elements/prompt-input";
import type { ChatImage } from "./images";

export function ImagePreview({ image, kind }: { image: ChatImage; kind?: "generated" }) {
  const [url,setURL]=useState(image.url || "");
  const [failed,setFailed]=useState(false);
  const [loaded,setLoaded]=useState(false);
  const generated=kind === "generated" || !!image.url?.includes("/image-hub/results/");
  const [downloading,setDownloading]=useState(false);
  const [downloadError,setDownloadError]=useState("");
  async function downloadImage() {
    if (!url || downloading) return;
    setDownloading(true); setDownloadError("");
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error("download failed");
      const blob = await response.blob();
      const objectURL = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const extension = blob.type.includes("jpeg") ? "jpg" : blob.type.includes("webp") ? "webp" : "png";
      const name = (image.name || "生成图片").replace(/[\\/:*?"<>|]/g, "-");
      link.href = objectURL;
      link.download = /\.(png|jpe?g|webp|gif)$/i.test(name) ? name : `${name}.${extension}`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(objectURL), 30000);
    } catch { setDownloadError("下载失败，请重试"); }
    finally { setDownloading(false); }
  }
  useEffect(()=>{let active=true;setURL(image.url || "");setFailed(false);setLoaded(false);void imageURL(image).then((value)=>{if(active)setURL(value);}).catch(()=>{if(active)setFailed(true);});return()=>{active=false;};},[image.url,image.assetID,image.clientID]);
  return <Dialog><DialogTrigger asChild>
    <button type="button" className={`image-preview${generated ? " generated-image-preview" : ""}`} data-image-loading={generated && !loaded && !failed} data-image-ready={generated && loaded && !failed} aria-label={`预览图片：${image.name}`}>
      {generated && !url && !failed ? <ImageGenerationLoading phase="loading" /> : null}
      {url ? <img src={url} alt={image.name} loading={generated ? "eager" : "lazy"} onLoad={()=>setLoaded(true)} onError={()=>setFailed(true)} /> : <span className="image-loading">{failed ? "加载失败" : "加载图片…"}</span>}
      {generated && failed ? <span className="image-loading">图片加载失败，点击查看原图</span> : null}
    </button>
  </DialogTrigger><DialogContent className="image-dialog" showCloseButton={false} aria-describedby={undefined}>
    <DialogTitle className="sr-only">{image.name}</DialogTitle>
    {url ? <img src={url} alt={image.name} /> : <span>{failed ? "图片加载失败，请检查网络" : "正在加载图片…"}</span>}
    <div className="image-dialog-actions">
      {downloadError && <span role="alert">{downloadError}</span>}
      <Button type="button" variant="ghost" className="image-download-circle" aria-label={downloading ? "正在下载" : "下载原图"} title={downloading ? "正在下载" : "下载原图"} disabled={!url || downloading} onClick={downloadImage}>
        {downloading ? <LoaderCircle className="size-5 animate-spin" /> : <Download className="size-5" />}
      </Button>
    </div>
  </DialogContent></Dialog>;
}
export function ImageGallery({ images }: { images: ChatImage[] }) {
  return <div className="message-images">{images.map((image, index) => <ImagePreview key={index} image={image} />)}</div>;
}
export function ImageAttachments({ disabled, maxFiles = 4 }: { disabled: boolean; maxFiles?: number }) {
  const { files, remove } = usePromptInputAttachments();
  if (!files.length) return null;
  return <div className="image-attachments" aria-label="待发送图片">{files.map((file) =>
    <div className="image-attachment" key={file.id}>
      <ImagePreview image={{ url: file.url, name: file.filename || "图片", mediaType: file.mediaType }} />
      <button type="button" className="image-remove" disabled={disabled} aria-label={`移除图片：${file.filename || "图片"}`} onClick={() => remove(file.id)}><X size={12} /></button>
    </div>
  )}<span className="attachment-count">{files.length}/{maxFiles}</span></div>;
}
export function UploadImageButton({ disabled, maxFiles = 4 }: { disabled: boolean; maxFiles?: number }) {
  const { openFileDialog } = usePromptInputAttachments();
  return <Button type="button" variant="ghost" size="icon" className="upload-image" disabled={disabled}
    aria-label="上传图片" title={`批量上传图片 · 每张最多 5 MB，最多 ${maxFiles} 张；不支持识图的模型自动使用识图工具`} onClick={openFileDialog}>
    <ImagePlus className="size-4" />
  </Button>;
}
