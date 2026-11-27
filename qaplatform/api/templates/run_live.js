(() => {
  const status = document.getElementById('status');
  const stream = document.getElementById('log');
  const list = document.getElementById('activity-events');
  const live = document.getElementById('activity-live');
  const terminal = value => value === 'completed' || value === 'failed';
  const label = value => value.replace(/[_-]/g, ' ').replace(/^./, char => char.toUpperCase());
  const eventKey = event => JSON.stringify([event.ts, event.level, event.stage, event.message]);
  const make = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = text;
    return element;
  };

  stream.scrollTop = stream.scrollHeight;
  if (terminal(status.dataset.status)) return;

  const url = '/api/runs/' + encodeURIComponent({{ run.id | tojson }});
  let keys = Array.from(list.children, row => eventKey({
    ts: row.querySelector('time').dateTime,
    level: row.dataset.level,
    stage: row.querySelector('.activity-stage').dataset.stage,
    message: row.querySelector('.activity-message').textContent,
  }));

  function eventRow(event) {
    const row = make('li', 'activity-entry', '');
    row.dataset.level = event.level;
    const time = make('time', 'activity-time', event.ts.slice(11, 19));
    time.dateTime = event.ts;
    const stage = make('span', 'activity-stage', label(event.stage || 'Run'));
    stage.dataset.stage = event.stage;
    if (event.level === 'warn' || event.level === 'error') {
      stage.append(' ', make('span', 'activity-level', event.level === 'warn' ? 'Warning' : 'Error'));
    }
    row.append(time, stage, make('span', 'activity-message', event.message));
    return row;
  }

  function showEvents(events) {
    const recent = events.slice(-60);
    const next = recent.map(eventKey);
    if (keys.length === next.length && keys.every((key, i) => key === next[i])) return;

    // Reuse entries that stay in the recent window so older visible rows do not jump.
    let overlap = Math.min(keys.length, next.length);
    while (overlap && !keys.slice(-overlap).every((key, i) => key === next[i])) overlap--;
    const follow = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 24;
    const expired = keys.length - overlap;
    const retained = list.children[expired];
    const previousPosition = retained?.getBoundingClientRect().top;
    for (let i = 0; i < expired; i++) list.firstElementChild.remove();
    list.append(...recent.slice(overlap).map(eventRow));
    keys = next;

    document.getElementById('activity-count').textContent = next.length + (next.length === 1 ? ' event' : ' events');
    document.getElementById('activity-empty').hidden = next.length > 0;
    if (follow) {
      stream.scrollTop = stream.scrollHeight;
    } else if (retained) {
      stream.scrollTop += retained.getBoundingClientRect().top - previousPosition;
    } else {
      stream.scrollTop = 0;
    }
  }

  function showRun(run) {
    const progress = Math.max(0, Math.min(100, run.progress));
    document.getElementById('progress').setAttribute('aria-valuenow', progress);
    document.getElementById('bar').style.width = progress + '%';
    document.getElementById('progress-value').textContent = progress + '%';
    if (status.dataset.status !== run.status) {
      status.dataset.status = run.status;
      status.textContent = label(run.status);
    }
    document.getElementById('stage').textContent = label(run.stage || run.status);
    document.getElementById('message').textContent = run.message || '';
    const worker = document.getElementById('no-worker');
    worker.hidden = !run.waiting_for_worker;
    if (run.waiting_for_worker) document.getElementById('no-worker-cmd').textContent = run.waiting_for_worker;
  }

  async function readJson(endpoint) {
    const response = await fetch(endpoint, {cache: 'no-store'});
    if (!response.ok) throw new Error('Run update failed');
    return response.json();
  }

  async function tick() {
    try {
      const [run, events] = await Promise.all([readJson(url), readJson(url + '/events?limit=60')]);
      showRun(run);
      showEvents(events);
      if (terminal(run.status)) {
        window.location.reload();
        return;
      }
      live.textContent = 'Live';
      live.dataset.state = 'live';
    } catch {
      live.textContent = 'Reconnecting';
      live.dataset.state = 'retry';
    }
    window.setTimeout(tick, 4000);
  }

  window.setTimeout(tick, 4000);
})();
