const form = document.querySelector('#submission-form');
const requestInput = document.querySelector('#request-id');
const runInput = document.querySelector('#run-id');
const status = document.querySelector('#run-status');
const output = document.querySelector('#run-output');
const runtime = document.querySelector('#agent-status');
const tokenInput = document.querySelector('#grip-token');
const workspaceInput = document.querySelector('#workspace-uri');
const protocol = location.protocol === 'https:' ? 'https-json.v1' : 'loom-http-json.v1';

// Connection credentials stay in this page's input; never persist them in browser storage.
async function exchange(method, target, fields = {}, endpoint = '/.well-known/grip') {
  if (!tokenInput.value) throw new Error('Enter your workspace token');
  const url = new URL(endpoint, location.href);
  if (url.origin !== location.origin) throw new Error('The workspace binding must use this Observer origin');
  const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, '0')).join('');
  const response = await fetch(url, {
    method: 'POST', redirect: 'error', credentials: 'omit',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({version: 'grip.v1', request_id: requestId, method, target,
      auth: {scheme: 'bearer', credential: tokenInput.value, tenant: workspaceInput.value.trim()},
      accepted_protocols: [protocol], ...fields}),
  });
  if (!response.ok) throw new Error(`Observer request failed (${response.status})`);
  const message = await response.json();
  if (message.version !== 'grip.v1' || message.request_id !== requestId) throw new Error('Invalid Observer reply');
  if (message.kind === 'error') throw new Error(message.error_code || 'GRIP request failed');
  if (message.kind !== 'reply' || !message.body) throw new Error('Invalid Observer reply');
  return message.body;
}

async function invoke(target, name, input = {}) {
  const description = await exchange('DESCRIBE', target);
  if (description.resource.uri !== target) throw new Error('Resource identity mismatch');
  const actionUri = `${workspaceInput.value.trim()}/actions/${name}/v1`;
  const offer = description.offers.find(item => item.action.uri === actionUri);
  const binding = offer?.bindings.find(item => item.protocol === protocol);
  if (!binding) throw new Error(`Action unavailable: ${name}`);
  return exchange('INVOKE', target, {action: offer.action.uri, input}, binding.endpoint || '/.well-known/grip');
}

async function loadDriver() {
  const directory = await invoke(workspaceInput.value.trim(), 'list-resources', {mcue: 'computing/driver'});
  const driver = directory.resources.find(resource => resource.uri === `${workspaceInput.value.trim()}/drivers/default`);
  if (!driver) throw new Error('Execution Driver resource not found');
  runtime.textContent = driver.metadata.available ? 'Execution Driver ready' : 'Driver offline; submissions can queue';
  return driver.uri;
}

async function loadRun(runUri) {
  const value = await invoke(runUri, 'read', {include_history: true});
  status.textContent = value.state || 'unknown';
  output.textContent = JSON.stringify(value, null, 2);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const key = requestInput.value.trim();
  const planText = document.querySelector('#plan').value.trim();
  if (!key || !planText) return;
  try {
    const driver = await loadDriver();
    const receipt = await invoke(driver, 'submit-plan', {idempotency_key: key, plan: JSON.parse(planText)});
    if (receipt.rejected) {
      output.textContent = JSON.stringify(receipt.rejected, null, 2);
      throw new Error(receipt.rejected.code);
    }
    runInput.value = receipt.run;
    await loadRun(receipt.run);
  } catch (error) {
    status.textContent = error.message;
  }
});

document.querySelector('#refresh-run').addEventListener('click', async () => {
  if (!runInput.value.trim()) return;
  try { await loadRun(runInput.value.trim()); } catch (error) { status.textContent = error.message; }
});

runtime.textContent = 'Connect to submit or inspect a Run';
