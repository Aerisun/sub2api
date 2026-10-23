export async function readGenerationStream(response: Response, receive: (data: any) => boolean): Promise<void> {
  if (!response.body) throw new Error("回复连接暂时中断");
  const reader=response.body.getReader();const decoder=new TextDecoder();let buffer="";
  try {
    for(;;){let timer:ReturnType<typeof setTimeout> | undefined;
      const {done,value}=await Promise.race([reader.read(),new Promise<never>((_,reject)=>{timer=setTimeout(()=>reject(new Error("回复连接暂时中断")),25000);})]).finally(()=>clearTimeout(timer));buffer+=decoder.decode(value,{stream:!done});
      buffer=buffer.replace(/\r\n/g,"\n");
      let boundary:number;
      while((boundary=buffer.indexOf("\n\n"))!==-1){const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);
        const payload=frame.split("\n").filter(line=>line.startsWith("data:")).map(line=>line.slice(5).trimStart()).join("\n");
        if(!payload)continue;const data=JSON.parse(payload);if(data.error)throw new Error(data.error);if(receive(data))return;
      }
      if(done)throw new Error("回复连接暂时中断");
    }
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
}
