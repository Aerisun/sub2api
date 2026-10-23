import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readGenerationStream} from './generation-stream.ts';
test('generation reconnection stream handles fragmented UTF-8 and ends only on job completion',async()=>{
 const encoder=new TextEncoder();const wire=encoder.encode(': keepalive\n\ndata: {"generation":{"status":"running"},"content":"你好🌟"}\n\ndata: {"generation":{"status":"completed"}}\n\n');
 const response=new Response(new ReadableStream({start(c){for(let i=0;i<wire.length;i+=3)c.enqueue(wire.slice(i,i+3));c.close();}}));const messages=[];
 await readGenerationStream(response,data=>{messages.push(data);return data.generation.status==='completed';});assert.equal(messages[0].content,'你好🌟');assert.equal(messages.length,2);
});
test('unplanned stream closure triggers reconnect rather than marking the generation complete',async()=>{
 await assert.rejects(readGenerationStream(new Response('data: {"generation":{"status":"running"},"content":"partial"}\n\n'),()=>false),/连接暂时中断/);
});
test('a silent SSE reader is released after 25 seconds so the durable job can be resumed',async(t)=>{
 t.mock.timers.enable({apis:['setTimeout']});
 let cancelled=false;
 const response=new Response(new ReadableStream({cancel(){cancelled=true;}}));
 const pending=assert.rejects(readGenerationStream(response,()=>false),/连接暂时中断/);
 t.mock.timers.tick(25000);
 await pending;
 assert.equal(cancelled,true);
});
