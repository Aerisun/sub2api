import { test } from "node:test";
import assert from "node:assert/strict";
import { searchQueries, searchSources, searchTime, toolState } from "./search-tool-state.ts";

test("search states map to AI SDK tool states", () => {
  assert.equal(toolState({status:"running"}), "input-available");
  assert.equal(toolState({status:"completed"}), "output-available");
  assert.equal(toolState({status:"failed"}), "output-error");
});
test("only valid public link schemes appear in source cards, without duplicates", () => {
  const tool={sources:[{url:"javascript:alert(1)"},{url:"data:text/html,hello"},{url:"https://u:p@example.com"},{url:"invalid"},{url:"https://example.com/docs",title:"Docs"},{url:"https://example.com/docs",title:"Duplicate"}]};
  assert.deepEqual(searchSources(tool), [tool.sources[4]]);
  assert.deepEqual(searchSources({}), []);
});
test("queries retain source filters and survive empty/invalid state", () => {
  assert.deepEqual(searchQueries({input:{query:"文档"}}), [{query:"文档"}]);
  const query={query:"官方文档",domains:["example.com"],recency_days:7};
  assert.deepEqual(searchQueries({input:{queries:[query,{query:""},{query:null}]}}), [query]);
});
test("search source cards retain larger results and link fragments", () => {
  const sources=Array.from({length:30},(_,i)=>({id:String(i),title:`Source ${i}`,url:`https://example.com/${i}#section`}));
  assert.deepEqual(searchSources({sources}), sources);
});
test("live retrieval time is distinct from publication time, with safe legacy fallback", () => {
  assert.match(searchTime({metadata:{searched_at:"2026-10-21T01:30:45.000Z"}}), /09:30:45/);
  assert.equal(searchTime({}), null);
  assert.equal(searchTime({metadata:{searched_at:"invalid"}}), null);
});
