import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';

const [browser, profile, url, control, attacker] = process.argv.slice(2);
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
  const navigate = async url => {
    await send('Page.navigate', { url });
    for (let i = 0; i < 100; i++) {
      const result = await send('Runtime.evaluate', {
        expression: `location.href === ${JSON.stringify(url)} && document.readyState === 'complete'`,
        returnByValue: true,
      });
      if (result.result.value) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(`Navigation did not complete: ${url}`);
  };
  await navigate(url);
  const images = await send('Runtime.evaluate', {
    expression: `Promise.all([...document.images].map(img => img.decode().then(() => img.naturalWidth, () => 0)))`,
    awaitPromise: true, returnByValue: true,
  });
  await navigate(attacker);
  const attack = await send('Runtime.evaluate', {
    expression: `fetch(${JSON.stringify(control)}, {method:'POST', headers:{'X-Workdash':'1', 'Content-Type':'application/json'}, body:'{}'}).then(() => 'allowed', () => 'blocked')`,
    awaitPromise: true, returnByValue: true,
  });
  console.log(JSON.stringify({ images: images.result.value, attack: attack.result.value }));
  socket.close();
} finally {
  chrome.kill();
}
