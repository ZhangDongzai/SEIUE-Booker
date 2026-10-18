/* 离线验证 WebUI 的 exec 兼容层与状态读取：
   模拟多种 KernelSU 桥接形态，确认页面都能自动选中可用通道、正确渲染状态。
   运行： node tests/webui_exec_test.js  */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "kernelsu-module", "webroot", "index.html"), "utf8");
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const SNAPSHOT = {
  STATE: "running", PID: "12345", PORT: "8080", HOST: "0.0.0.0",
  PY_READY: "yes", APP_READY: "yes", DATA: "/data/adb/badminton-booker/data",
  AT: stamp(),
};

function makeDom() {
  const els = {};
  const get = (id) => {
    if (!els[id]) {
      els[id] = {
        id, textContent: "", innerHTML: "", className: "", disabled: false,
        style: {}, classList: { add() {}, remove() {} }, onclick: null, _t: null,
      };
    }
    return els[id];
  };
  return { els, document: { getElementById: get, addEventListener() {}, hidden: false } };
}

function boot(ksuImpl, snapshot) {
  const { els, document } = makeDom();
  const win = { ksu: ksuImpl };
  if (ksuImpl) ksuImpl.__win = win;      // 官方契约：原生回调 window[callbackName]
  const fetchStub = () => Promise.resolve({ ok: true, json: () => Promise.resolve(snapshot) });
  // eslint-disable-next-line no-new-func
  new Function("window", "document", "fetch", "navigator", "console", code)(
    win, document, fetchStub, { clipboard: null }, console);
  return els;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const OUT = (cmd) => (cmd.includes("echo __KSU_OK__") ? "__KSU_OK__\n" : "uid=0(root) gid=0(root)\n");

const shapes = {
  "三参数回调，opts 是对象": {
    exec(cmd, opts, cb) {
      if (typeof cb !== "function") throw new Error("bad args");
      setTimeout(() => cb(0, OUT(cmd), ""), 5);
    },
  },
  "单参数回调（一个 JSON 字符串）": {
    exec(cmd, opts, cb) {
      if (typeof cb !== "function") return;          // 别的调用形态不会带回调，忽略即可
      setTimeout(() => cb(JSON.stringify({ errno: 0, stdout: OUT(cmd), stderr: "" })), 5);
    },
  },
  "两参数 exec(cmd, cb)": {
    exec(cmd, cb) {
      if (typeof cb !== "function") throw new Error("bad args");
      setTimeout(() => cb(0, OUT(cmd), ""), 5);
    },
  },
  "只返回 Promise": {
    exec(cmd) { return Promise.resolve({ errno: 0, stdout: OUT(cmd), stderr: "" }); },
  },
  "同步返回字符串": {
    exec(cmd) { return JSON.stringify({ errno: 0, stdout: OUT(cmd), stderr: "" }); },
  },
  /* 官方契约（npm kernelsu@3）：exec(cmd, JSON字符串, 回调名) + window[名称] */
  "官方契约（回调名 + window[name]）": {
    exec(cmd, opts, cbName) {
      if (typeof opts !== "string") throw new Error("options 必须是字符串");
      if (typeof cbName !== "string") throw new Error("callback 必须是函数名");
      setTimeout(() => {
        const fn = this.__win && this.__win[cbName];
        if (typeof fn === "function") fn(0, OUT(cmd), "");
      }, 20);
    },
  },

  /* 官方契约的 spawn：exec 直接抛异常，只能靠 spawn 跑通 */
  "官方 spawn（exec 抛异常）": {
    exec() { throw new Error("Error invoking exec: Java exception was raised"); },
    spawn(shell, argsJson, optsJson, childName) {
      const args = JSON.parse(argsJson);
      const cmd = args[args.length - 1];
      const child = this.__win && this.__win[childName];
      if (!child) throw new Error("没有登记子进程对象");
      setTimeout(() => {
        child.stdout.emit("data", OUT(cmd));
        child.emit("exit", 0);
      }, 25);
    },
  },

  /* exec 彻底不可用，但 spawn 还活着——独立兜底通路 */
  "exec 全坏但 spawn 可用": {
    exec() { throw new Error("Error invoking exec: Java exception was raised"); },
    spawn(shell, args, opts) {
      const cmd = args[args.length - 1];
      return {
        stdout: { on(ev, fn) { if (ev === "data") setTimeout(() => fn(OUT(cmd)), 10); } },
        stderr: { on() {} },
        on(ev, fn) { if (ev === "exit") setTimeout(() => fn(0), 25); },
      };
    },
  },

  /* 用户的真机形态：opts 传对象直接抛 Java 异常；传字符串能跑，但第一次回调要 6 秒。
     旧版串行探测给的 4 秒窗口会把它判死——这正是上一版报「超时无响应」的原因。 */
  "对象参数抛异常 + 字符串参数但回调要 6 秒": {
    exec(cmd, opts, cb) {
      if (opts && typeof opts === "object") {
        throw new Error("Error invoking exec: Java exception was raised during method invocation");
      }
      if (typeof cb !== "function") throw new Error("bad callback");
      setTimeout(() => cb(0, OUT(cmd), ""), 6000);
    },
  },
};

(async () => {
  let pass = 0, fail = 0;
  const check = (name, ok, extra) => {
    if (ok) { pass++; console.log("  ok   " + name); }
    else { fail++; console.log("  FAIL " + name + (extra ? "  → " + String(extra).slice(0, 140) : "")); }
  };

  for (const [label, impl] of Object.entries(shapes)) {
    console.log("\n[" + label + "]");
    const slow = label.includes("6 秒");
    const els = boot(impl, SNAPSHOT);
    // 首屏走快照，不等命令通道，所以这里只需等微任务
    await wait(400);

    check("状态渲染为运行中", els.stateText.textContent === "运行中", els.stateText.textContent);
    check("端口取自快照", els.vPort.textContent === "8080", els.vPort.textContent);
    check("PID 取自快照", els.vPid.textContent === "12345", els.vPid.textContent);
    check("运行时显示就绪", els.vPy.textContent === "就绪", els.vPy.textContent);

    els.btnSelf.onclick();
    await wait(slow ? 14000 : 500);   // 慢桥接：探测 6s + 自检命令 6s
    const log = els.log.textContent || "";
    check("自检报告了可用 exec 变体", /exec = (ksu|global|spawn)-/.test(log), log.slice(0, 100));
    check("自检拿到 root 身份", log.includes("uid=0"), log.slice(0, 140));
  }

  console.log("\n[所有通道都不可用]");
  const els = boot({ exec() { throw new Error("not supported"); } }, SNAPSHOT);
  await wait(1200);
  const banner = els.bannerBox.innerHTML || "";
  check("提示命令通道不可用", banner.includes("命令通道不可用"), banner.slice(0, 100));
  check("列出每种尝试结果", (banner.match(/→/g) || []).length >= 5,
        String((banner.match(/→/g) || []).length));
  check("exec 坏掉时仍能显示状态", els.stateText.textContent === "运行中", els.stateText.textContent);

  els.btnStart.onclick();
  await wait(600);
  check("点启动时说明是通道问题而非缺脚本",
        (els.log.textContent || "").includes("命令通道不可用"),
        (els.log.textContent || "").slice(0, 80));

  console.log("\n[完全没有 ksu 对象，例如普通浏览器]");
  const els2 = boot(undefined, SNAPSHOT);
  await wait(200);
  check("提示需在 KernelSU 中打开", (els2.bannerBox.innerHTML || "").includes("KernelSU 管理器"),
        (els2.bannerBox.innerHTML || "").slice(0, 80));
  check("按钮被禁用", els2.btnStart.disabled === true);

  console.log("\n通过 " + pass + " 项，失败 " + fail + " 项");
  process.exit(fail ? 1 : 0);
})();
