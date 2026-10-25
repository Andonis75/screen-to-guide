/* Run with: node --test tests/test_lesson_playback.cjs
 * Executes the actual HTML template, including its generated chapter/image
 * buttons. The media element and clock are deterministic; no browser package,
 * network request, or video fixture is required.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const template = fs.readFileSync(path.join(__dirname, "../assets/lesson.html"), "utf8");
const script = [...template.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(source => source.includes('"use strict"'));
assert.ok(script, "lesson.html must contain its application script");

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.textContent = "";
    this.className = "";
    this.hidden = false;
    this.open = false;
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? (name === "src" ? this.src || null : null); }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(callback);
  }
  removeEventListener(name, callback) { this.listeners.get(name)?.delete(callback); }
  emit(name, properties = {}) {
    const event = { type: name, target: this, ...properties };
    for (const callback of [...(this.listeners.get(name) || [])]) callback(event);
  }
  click() { this.emit("click"); }
  showModal() { this.open = true; }
  close() { this.open = false; this.emit("close"); }
  getBoundingClientRect() { return { left: 0, top: 0, right: 100, bottom: 100 }; }
}

class Clock {
  constructor() { this.nextId = 1; this.pending = new Map(); this.now = 0; }
  schedule(callback, kind, delay = 0) {
    const id = this.nextId++;
    this.pending.set(id, { id, callback, kind, delay });
    return id;
  }
  cancel(id) { this.pending.delete(id); }
  snapshot() { return [...this.pending.values()]; }
  invoke(job, video) {
    job.callback(this.now, { mediaTime: video.currentTime, presentedFrames: 1 });
  }
  tick(video, milliseconds = 20) {
    this.now += milliseconds;
    // Only run the current generation; monitors may enqueue their next tick.
    for (const job of this.snapshot()) {
      if (!this.pending.has(job.id)) continue;
      if (job.kind !== "interval") this.pending.delete(job.id);
      this.invoke(job, video);
    }
  }
}

class Video extends Element {
  constructor(clock) {
    super("video");
    this.clock = clock;
    this.currentTime = 0;
    this.duration = 910;
    this.readyState = 4;
    this.paused = true;
    this.ended = false;
    this.playbackRate = 1;
    this.seeking = false;
    this.playCount = 0;
  }
  load() { this.loadCount = (this.loadCount || 0) + 1; }
  play() {
    this.playCount++;
    if (this.paused) {
      this.paused = false;
      this.ended = false;
      this.emit("play");
      this.emit("playing");
    }
    return Promise.resolve();
  }
  pause() {
    if (!this.paused) { this.paused = true; this.emit("pause"); }
  }
  seek(seconds) {
    this.seeking = true;
    this.currentTime = seconds;
    this.emit("seeking");
    this.seeking = false;
    this.emit("seeked");
  }
  requestVideoFrameCallback(callback) { return this.clock.schedule(callback, "video-frame"); }
  cancelVideoFrameCallback(id) { this.clock.cancel(id); }
}

function createPlayer({ readyState = 4 } = {}) {
  const clock = new Clock(), video = new Video(clock), elements = new Map();
  video.readyState = readyState;
  for (const match of template.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element());
  elements.set("video", video);
  const chapter = (title, start, end, screenshot) => ({
    title, start_seconds: start, end_seconds: end, paragraphs: [], bullets: [],
    steps: [], code_blocks: [], note: "", images: screenshot === undefined ? [] : [{
      timestamp_seconds: screenshot, path: "frames/example.jpg", caption: "配置界面",
    }],
  });
  elements.get("lesson-data").textContent = JSON.stringify({
    markdown: "Test lesson",
    lesson: {
      title: "MCP 教学", summary: "测试章节播放范围", video: "source.mkv",
      source: { name: "source.mkv", duration_seconds: 910, width: 1920, height: 1080 },
      prerequisites: [], glossary: [], references: [],
      sections: [chapter("在 Cline 中配置 MCP", 223, 330, 240), chapter("下一章", 400, 480)],
    },
  });
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement: name => new Element(name),
    body: new Element("body"), images: [], querySelectorAll: () => [],
  };
  const globals = {
    console, document, performance: { now: () => clock.now },
    setTimeout: (fn, delay) => clock.schedule(fn, "timeout", delay),
    clearTimeout: id => clock.cancel(id),
    setInterval: (fn, delay) => clock.schedule(fn, "interval", delay),
    clearInterval: id => clock.cancel(id),
    requestAnimationFrame: fn => clock.schedule(fn, "animation-frame"),
    cancelAnimationFrame: id => clock.cancel(id),
    URL, Blob,
  };
  globals.window = globals;
  const context = vm.createContext(globals);
  vm.runInContext(script, context, { filename: "lesson.html" });
  const descendants = element => element.children.flatMap(child =>
    typeof child === "object" ? [child, ...descendants(child)] : []);
  const chapterButtons = descendants(elements.get("chapters"))
    .filter(element => element.className === "jump");
  const imageButtons = descendants(elements.get("chapters"))
    .filter(element => element.className === "figure-time");
  return {
    clock, video, elements, chapterButtons, imageButtons,
    open(start, end, title = "测试章节") {
      context.testArgs = [start, end, title];
      vm.runInContext("openVideo(...testArgs)", context);
    },
    reach(seconds) { video.currentTime = seconds; video.emit("timeupdate"); },
  };
}

test("generated chapter button starts its chapter and clamps playback to the exact end", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  assert.equal(p.video.currentTime, 223);
  assert.equal(p.video.paused, false);
  assert.equal(p.elements.get("player").open, true);
  assert.equal(p.elements.get("player-title").textContent, "在 Cline 中配置 MCP");
  p.reach(330.27);
  assert.equal(p.video.paused, true, "chapter playback must stop at the endpoint");
  assert.equal(p.video.currentTime, 330, "timeupdate overshoot must be corrected");
});

test("replaying a completed chapter restarts its selected range and remains bounded", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  p.reach(330);
  p.video.play();
  assert.equal(p.video.currentTime, 223);
  assert.equal(p.video.paused, false);
  p.reach(331);
  assert.equal(p.video.paused, true);
  assert.equal(p.video.currentTime, 330);
});

test("pausing and resuming inside a chapter preserves the current position", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  p.reach(260);
  p.video.pause();
  p.video.play();
  assert.equal(p.video.currentTime, 260);
  assert.equal(p.video.paused, false);
});

test("screenshot playback retains its whole chapter range and the replay button restarts the chapter", () => {
  const p = createPlayer();
  p.imageButtons[0].click();
  assert.equal(p.video.currentTime, 240);
  p.video.seek(230);
  assert.equal(p.video.currentTime, 230, "screenshot playback may seek back within its chapter");
  p.video.seek(100);
  assert.equal(p.video.currentTime, 223);
  p.reach(330.15);
  assert.equal(p.video.paused, true, "screenshot playback must retain its chapter end");
  assert.equal(p.video.currentTime, 330);
  p.elements.get("replay-chapter").click();
  assert.equal(p.video.currentTime, 223);
  assert.equal(p.video.paused, false);
  p.reach(330.2);
  assert.equal(p.video.paused, true);
  assert.equal(p.video.currentTime, 330);
});

test("seeking stays inside the selected range and reaching the end pauses", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  p.video.seek(100);
  assert.equal(p.video.currentTime, 223);
  p.video.seek(280);
  assert.equal(p.video.currentTime, 280);
  p.video.seek(500);
  assert.equal(p.video.currentTime, 330);
  assert.equal(p.video.paused, true);
});

test("full-video control explicitly removes the previous chapter boundary", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  p.reach(330);
  p.elements.get("watch-all").click();
  assert.equal(p.video.currentTime, 0);
  p.reach(500);
  p.clock.tick(p.video);
  assert.equal(p.video.paused, false);
  assert.equal(p.video.currentTime, 500);
  p.video.seek(100);
  assert.equal(p.video.currentTime, 100);
});

test("the in-player full-video button permits continuing beyond the chapter end", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  p.reach(330);
  p.elements.get("continue-video").click();
  assert.equal(p.video.paused, false);
  p.reach(500);
  p.clock.tick(p.video);
  assert.equal(p.video.paused, false);
  assert.equal(p.video.currentTime, 500);
  p.video.seek(100);
  assert.equal(p.video.currentTime, 100);
});

test("active boundary monitor stops playback even before the next timeupdate event", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  assert.ok(p.clock.pending.size > 0, "bounded playback needs an active boundary monitor");
  p.video.currentTime = 330.04;
  p.clock.tick(p.video);
  assert.equal(p.video.paused, true);
  assert.equal(p.video.currentTime, 330);
});

test("switching chapters cancels old callbacks without stopping the new chapter", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  const previousCallbacks = p.clock.snapshot();
  assert.ok(previousCallbacks.length > 0, "this regression must exercise a queued callback");
  p.chapterButtons[1].click();
  p.video.currentTime = 410;
  for (const callback of previousCallbacks) p.clock.invoke(callback, p.video);
  p.video.emit("timeupdate");
  assert.equal(p.video.paused, false);
  assert.equal(p.video.currentTime, 410);
  p.reach(480.1);
  assert.equal(p.video.paused, true);
  assert.equal(p.video.currentTime, 480);
});

test("closing the player stops it, cancels monitoring, and makes queued callbacks harmless", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  const previousCallbacks = p.clock.snapshot();
  assert.ok(previousCallbacks.length > 0, "this regression must exercise a queued callback");
  p.elements.get("close-video").click();
  assert.equal(p.elements.get("player").open, false);
  assert.equal(p.video.paused, true);
  assert.equal(p.clock.pending.size, 0, "closing must release the playback monitor");
  p.video.currentTime = 340;
  for (const callback of previousCallbacks) p.clock.invoke(callback, p.video);
  p.video.emit("timeupdate");
  assert.equal(p.video.currentTime, 340);
  assert.equal(p.video.paused, true);
  assert.equal(p.clock.pending.size, 0);
});

test("only the latest chapter request starts when media metadata arrives", () => {
  const p = createPlayer({ readyState: 0 });
  p.chapterButtons[0].click();
  p.chapterButtons[1].click();
  assert.equal(p.video.paused, true);
  p.video.readyState = 1;
  p.video.emit("loadedmetadata");
  assert.equal(p.video.currentTime, 400);
  assert.equal(p.video.paused, false);
  p.reach(480.1);
  assert.equal(p.video.currentTime, 480);
  assert.equal(p.video.paused, true);
});

test("the last chapter uses the actual media end when the documented end is rounded up", () => {
  const p = createPlayer();
  p.open(900, 910.1, "最后一章");
  p.video.currentTime = p.video.duration;
  p.video.paused = true;
  p.video.ended = true;
  p.video.emit("ended");
  assert.equal(p.video.currentTime, 910);
  assert.equal(p.video.paused, true);
  assert.equal(p.clock.pending.size, 0);
  p.video.play();
  assert.equal(p.video.currentTime, 900);
  p.reach(910);
  assert.equal(p.video.paused, true);
});

test("changing playback speed keeps the chapter monitor active and bounded", () => {
  const p = createPlayer();
  p.chapterButtons[0].click();
  p.video.currentTime = 329.9;
  p.video.playbackRate = 2;
  p.video.emit("ratechange");
  assert.ok(p.clock.pending.size > 0);
  p.video.currentTime = 330.03;
  p.clock.tick(p.video);
  assert.equal(p.video.paused, true);
  assert.equal(p.video.currentTime, 330);
});

test("metadata arriving after the dialog closes does not restart playback", () => {
  const p = createPlayer({ readyState: 0 });
  p.chapterButtons[0].click();
  p.elements.get("close-video").click();
  p.video.readyState = 1;
  p.video.emit("loadedmetadata");
  assert.equal(p.video.playCount, 0);
  assert.equal(p.video.paused, true);
  assert.equal(p.elements.get("player").open, false);
  assert.equal(p.clock.pending.size, 0);
});
