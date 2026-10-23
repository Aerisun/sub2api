import { test } from "node:test";
import assert from "node:assert/strict";
import {
  modelProfile,
  reasoningParams,
  validEffort,
  chatParams,
} from "./model-settings.ts";
test("Fast is a separate opt-in field and never changes effort or another provider", () => {
  assert.deepEqual(chatParams("gpt-6.1-sol", "max"), {reasoning_effort:"max"});
  assert.deepEqual(chatParams("gpt-6.1-sol", "max", true), {reasoning_effort:"max",service_tier:"fast"});
  assert.deepEqual(chatParams("gpt-6-sol", "none", true), {reasoning_effort:"none",service_tier:"fast"});
  assert.deepEqual(chatParams("kimi-k3", "max", true), {reasoning_effort:"max"});
});
test("Codex chat preserves every supported effort and refuses unsupported Off", () => {
  for (const model of ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]) {
    assert.equal(validEffort(model, undefined), "medium");
    assert.equal(modelProfile(model).brand, "OpenAI");
    for (const effort of ["low", "medium", "high", "xhigh", "max"]) {
      assert.deepEqual(reasoningParams(model, effort), {reasoning_effort: effort});
    }
    const alwaysThinking = ["gpt-6.1-sol", "gpt-6-astra"].includes(model);
    assert.equal(modelProfile(model).efforts.includes("none"), !alwaysThinking);
    assert.deepEqual(reasoningParams(model, "none"), {reasoning_effort: alwaysThinking ? "medium" : "none"});
  }
});
test("Gemini web uses standard or extended thinking without forcing low to extended", () => {
  for (const model of ["gemini-web-flash-lite", "gemini-web-flash", "gemini-web-pro", "gemini-web-argon"]) {
    assert.equal(modelProfile(model).brand, "Gemini");
    assert.deepEqual(modelProfile(model).efforts, ["low", "high"]);
    assert.equal(validEffort(model, "none"), "low");
    assert.deepEqual(reasoningParams(model, "low"), {reasoning_effort:"low"});
    assert.deepEqual(reasoningParams(model, "high"), {reasoning_effort:"high"});
  }
});
test("current models expose their native tiers rather than gateway aliases", () => {
  assert.deepEqual(modelProfile("qwen3.8-max").efforts, [
    "none",
    "low",
    "medium",
    "xhigh",
  ]);
  assert.deepEqual(modelProfile("qwen3.8-flash").efforts, [
    "none",
    "low",
    "medium",
    "xhigh",
  ]);
  assert.deepEqual(modelProfile("glm-5.2").efforts, [
    "none",
    "high",
    "max",
  ]);
  assert.deepEqual(modelProfile("glm-5.3").efforts, [
    "low",
    "high",
    "max",
  ]);
  assert.deepEqual(modelProfile("kimi-k3").efforts, [
    "low",
    "high",
    "max",
  ]);
  assert.deepEqual(modelProfile("deepseek-v4.1-flash").efforts, [
    "none",
    "low",
    "high",
    "max",
  ]);
});
test("always-thinking models never receive a disable switch", () => {
  assert.equal(validEffort("kimi-k3", "none"), "low");
  assert.deepEqual(reasoningParams("glm-5.3", "none"), {reasoning_effort:"low",thinking:{type:"enabled"}});
  assert.deepEqual(reasoningParams("kimi-k3", "max"), {
    reasoning_effort: "max",
  });
});
test("off and on reach the correct provider controls", () => {
  assert.deepEqual(reasoningParams("qwen3.8-max", "none"), {
    reasoning_effort: "none",
    enable_thinking: false,
  });
  assert.deepEqual(reasoningParams("qwen3.8-max", "medium"), {
    reasoning_effort: "medium",
    enable_thinking: true,
  });
  assert.deepEqual(reasoningParams("glm-5.2", "none"), {
    reasoning_effort: "none",
    thinking: { type: "disabled" },
  });
  assert.deepEqual(reasoningParams("deepseek-v4.1-flash", "high"), {
    reasoning_effort: "high",
    thinking: { type: "enabled" },
  });
});
test("switching models cannot carry an unsupported effort into requests", () => {
  assert.deepEqual(reasoningParams("glm-5.2", "medium"), {reasoning_effort:"max",thinking:{type:"enabled"}});
  assert.deepEqual(reasoningParams("qwen3.8-max", "max"), {reasoning_effort:"xhigh",enable_thinking:true});
  assert.deepEqual(reasoningParams("unknown-model", "high"), {});
  assert.deepEqual(reasoningParams("qwen3.8-max", "default"), {reasoning_effort:"xhigh",enable_thinking:true});
});

test('new NVIDIA routes expose only verified controls', () => {
 assert.deepEqual(modelProfile('nemotron-3.5-lightning-30b-a3b').efforts, ['none','low','medium','high','xhigh','max']);
 assert.deepEqual(modelProfile('step-5-preview').efforts, []);
 assert.deepEqual(reasoningParams('step-5-preview','high'), {});
 assert.deepEqual(reasoningParams('nemotron-3.5-lightning-30b-a3b','high'), {reasoning_effort:'high',thinking:{type:'enabled'}});
});

test('legacy defaults resolve to the exact tier displayed and sent', () => {
 for (const [model, tier] of [['qwen3.8-flash','xhigh'], ['glm-5.2','max'], ['glm-5.3','low'], ['kimi-k3','low'], ['deepseek-v4.1-flash','low']]) {
  assert.equal(validEffort(model, 'default'), tier);
  assert.equal(reasoningParams(model, 'default').reasoning_effort, tier);
  assert.ok(!modelProfile(model).efforts.includes('default'));
 }
 assert.equal(validEffort('qwen3.8-flash', 'medium'), 'medium');
});
