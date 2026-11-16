import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';

const [browser, profile, url] = process.argv.slice(2);
const chrome = spawn(browser, [
  '--headless', '--no-sandbox', '--disable-gpu', '--no-first-run',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });
try {
  let port;
  for (let i = 0; i < 100; i++) {
    try {
      port = (await readFile(`${profile}/DevToolsActivePort`, 'utf8')).split('\n')[0];
      break;
    } catch {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
  }
  if (!port) throw new Error('Browser failed to start');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  const socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let id = 0;
  const pending = new Map();
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (pending.has(message.id)) {
      const [resolve, reject] = pending.get(message.id);
      pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    }
  };
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const request = ++id;
    pending.set(request, [resolve, reject]);
    socket.send(JSON.stringify({ id: request, method, params }));
  });
  const results = [];
  for (const width of [375, 667, 1024, 1200]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 800, deviceScaleFactor: 1, mobile: width < 1024 });
    if (width === 375) {
      await send('Page.navigate', { url });
      for (let i = 0; i < 100; i++) {
        const ready = await send('Runtime.evaluate', { expression: `location.href === ${JSON.stringify(url)} && document.readyState === 'complete'`, returnByValue: true });
        if (ready.result.value) break;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
    const result = await send('Runtime.evaluate', {
      expression: `(() => {
        const pre = document.querySelector('pre'), table = document.querySelector('table');
        pre.scrollLeft = 0; table.scrollLeft = 0;
        pre.scrollLeft = 100; table.scrollLeft = 100;
        const body = getComputedStyle(document.body), cell = getComputedStyle(document.querySelector('td'));
        return { viewport: innerWidth, page: document.documentElement.scrollWidth,
          preScroll: pre.scrollLeft, tableScroll: table.scrollLeft,
          bodyMax: body.maxWidth, bodyPadding: body.paddingLeft, bodyMargin: body.marginTop,
          cellPadding: cell.paddingLeft, preFont: getComputedStyle(pre).fontSize };
      })()`, returnByValue: true,
    });
    results.push(result.result.value);
  }
  console.log(JSON.stringify(results));
  socket.close();
} finally {
  chrome.kill();
}
