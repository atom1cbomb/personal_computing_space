const form = document.querySelector('#submission-form');
const requestInput = document.querySelector('#request-id');
const runInput = document.querySelector('#run-id');
const status = document.querySelector('#run-status');
const output = document.querySelector('#run-output');
const runtime = document.querySelector('#agent-status');

async function loadRuntime() {
  try {
    const response = await fetch('/api/v1/runtime');
    const value = await response.json();
    runtime.textContent = response.ok ? 'Execution Driver ready' : (value.code || 'Execution Driver unavailable');
  } catch (_error) {
    runtime.textContent = 'Execution Driver unavailable';
  }
}

async function loadRun(runId) {
  const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}`);
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || value.code || 'run lookup failed');
  status.textContent = value.state || 'unknown';
  output.textContent = JSON.stringify(value, null, 2);
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const requestId = requestInput.value.trim();
  const planText = document.querySelector('#plan').value.trim();
  if (!requestId || !planText) return;
  try {
    const response = await fetch('/api/v1/run-submissions', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({request_id: requestId, plan: JSON.parse(planText)}),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail || value.code || 'submission failed');
    runInput.value = value.run_id || '';
    await loadRun(value.run_id);
  } catch (error) {
    status.textContent = error.message;
  }
});

document.querySelector('#refresh-run').addEventListener('click', async () => {
  if (!runInput.value.trim()) return;
  try { await loadRun(runInput.value.trim()); } catch (error) { status.textContent = error.message; }
});

void loadRuntime();
