(function () {
  var formState = { dirty: false };
  var PREFS_KEY = 'control-nexus.ui';
  var prefs = { liveAgents: true, relativeTime: true, compact: false };
  var pageTimers = [];

  function trackTimer(timerId) {
    pageTimers.push(timerId);
    return timerId;
  }

  function clearPageTimers() {
    pageTimers.forEach(function (timerId) {
      window.clearInterval(timerId);
    });
    pageTimers = [];
  }

  try {
    var storedPrefs = JSON.parse(window.localStorage.getItem(PREFS_KEY) || '{}');
    if (storedPrefs && typeof storedPrefs === 'object') {
      prefs.liveAgents = storedPrefs.liveAgents !== false;
      prefs.relativeTime = storedPrefs.relativeTime !== false;
      prefs.compact = storedPrefs.compact === true;
    }
  } catch (error) {
    // Corrupt or unavailable storage: keep defaults.
  }

  function savePrefs() {
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch (error) {
      // Storage can be unavailable in private mode; preferences stay in-memory.
    }
  }

  function relativeLabel(isoValue) {
    if (!isoValue) {
      return '';
    }
    var then = new Date(isoValue);
    if (Number.isNaN(then.getTime())) {
      return '';
    }
    var delta = Math.max(0, Date.now() - then.getTime());
    var seconds = Math.floor(delta / 1000);
    if (seconds < 45) return 'just now';
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + 'm ago';
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + 'h ago';
    var days = Math.floor(hours / 24);
    if (days < 7) return days + 'd ago';
    return then.toLocaleDateString();
  }

  function renderRelativeTimes() {
    document.querySelectorAll('[data-ts]').forEach(function (el) {
      var isoValue = el.getAttribute('data-ts');
      if (!isoValue) {
        return;
      }
      if (el.dataset.tsOriginal === undefined) {
        el.dataset.tsOriginal = el.textContent;
      }
      if (!prefs.relativeTime) {
        el.textContent = el.dataset.tsOriginal;
        return;
      }
      var label = relativeLabel(isoValue);
      if (!label) {
        return;
      }
      var prefix = el.getAttribute('data-relative-prefix') || '';
      el.textContent = prefix + label;
      el.title = new Date(isoValue).toLocaleString();
    });
  }

  function updatePreferenceControls() {
    var live = document.getElementById('pref-live-agents');
    var relative = document.getElementById('pref-relative-time');
    var compact = document.getElementById('pref-compact');
    if (live) live.checked = prefs.liveAgents;
    if (relative) relative.checked = prefs.relativeTime;
    if (compact) compact.checked = prefs.compact;
  }

  function bindPreferenceControls() {
    var live = document.getElementById('pref-live-agents');
    var relative = document.getElementById('pref-relative-time');
    var compact = document.getElementById('pref-compact');
    if (live) {
      live.addEventListener('change', function () {
        prefs.liveAgents = live.checked;
        savePrefs();
        syncLiveBadge();
      });
    }
    if (relative) {
      relative.addEventListener('change', function () {
        prefs.relativeTime = relative.checked;
        savePrefs();
        renderRelativeTimes();
      });
    }
    if (compact) {
      compact.addEventListener('change', function () {
        prefs.compact = compact.checked;
        savePrefs();
        document.body.classList.toggle('compact', prefs.compact);
      });
    }
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var helper = document.createElement('textarea');
    helper.value = text;
    helper.setAttribute('readonly', 'readonly');
    helper.style.position = 'fixed';
    helper.style.opacity = '0';
    document.body.appendChild(helper);
    helper.select();
    document.execCommand('copy');
    helper.remove();
    return Promise.resolve();
  }

  function bindCopyButtons() {
    document.addEventListener('click', function (event) {
      var trigger = event.target.closest('[data-copy], [data-copy-target]');
      if (!trigger) {
        return;
      }
      var text = '';
      if (trigger.hasAttribute('data-copy')) {
        text = trigger.getAttribute('data-copy');
      } else {
        var target = document.getElementById(trigger.getAttribute('data-copy-target'));
        text = target ? target.textContent : '';
      }
      if (!text) {
        return;
      }
      var original = null;
      if (!trigger.classList.contains('copy-link')) {
        original = trigger.textContent;
      }
      copyText(text).then(function () {
        if (original !== null) {
          trigger.textContent = 'Copied';
        } else {
          trigger.classList.add('is-copied');
        }
        window.setTimeout(function () {
          if (original !== null) {
            trigger.textContent = original;
          } else {
            trigger.classList.remove('is-copied');
          }
        }, 1400);
      }).catch(function () {
        if (original !== null) {
          trigger.textContent = 'Copy failed';
          window.setTimeout(function () { trigger.textContent = original; }, 1400);
        }
      });
    });
  }

  function initFilters() {
    document.querySelectorAll('[data-filter-root]').forEach(function (root) {
      var textInput = root.querySelector('[data-filter-text]');
      var selects = Array.prototype.slice.call(root.querySelectorAll('[data-filter-attr]'));
      var rows = Array.prototype.slice.call(root.querySelectorAll('[data-filter-row]'));
      var empty = root.querySelector('[data-filter-empty]');
      if (!rows.length) {
        return;
      }
      function apply() {
        var query = textInput ? textInput.value.trim().toLowerCase() : '';
        var active = selects
          .map(function (select) {
            return {
              attr: select.getAttribute('data-filter-attr'),
              token: select.getAttribute('data-filter-match') === 'token',
              value: select.value
            };
          })
          .filter(function (filter) { return filter.value !== ''; });
        var visible = 0;
        rows.forEach(function (row) {
          var matched = !query || (row.getAttribute('data-search') || '').indexOf(query) !== -1;
          if (matched) {
            active.forEach(function (filter) {
              var cell = row.getAttribute('data-' + filter.attr) || '';
              if (filter.token) {
                if ((' ' + cell + ' ').indexOf(' ' + filter.value + ' ') === -1) {
                  matched = false;
                }
              } else if (cell !== filter.value) {
                matched = false;
              }
            });
          }
          row.hidden = !matched;
          if (matched) {
            visible += 1;
          }
        });
        if (empty) {
          empty.hidden = visible !== 0;
        }
      }
      if (textInput) {
        textInput.addEventListener('input', apply);
      }
      selects.forEach(function (select) {
        select.addEventListener('change', apply);
      });
      apply();
    });
  }

  function syncLiveBadge() {
    var badge = document.getElementById('agents-live-badge');
    if (!badge) {
      return;
    }
    badge.textContent = prefs.liveAgents ? 'Live' : 'Paused';
    badge.classList.toggle('is-off', !prefs.liveAgents);
    badge.title = prefs.liveAgents
      ? 'Agent status refreshes automatically every 20 seconds'
      : 'Live refresh paused. Click to resume.';
  }

  function initLiveAgents() {
    var table = document.querySelector('table[data-live-agents-url]');
    var badge = document.getElementById('agents-live-badge');
    if (badge) {
      syncLiveBadge();
      badge.addEventListener('click', function () {
        prefs.liveAgents = !prefs.liveAgents;
        savePrefs();
        syncLiveBadge();
        updatePreferenceControls();
      });
    }
    if (!table) {
      return;
    }
    var url = table.getAttribute('data-live-agents-url');

    function applyAgentRow(row, agent) {
      var status = row.querySelector('[data-agent-field="status"]');
      if (status) {
        status.textContent = agent.status;
        status.className = 'pill pill-' + agent.status;
      }
      var seen = row.querySelector('[data-agent-field="last_seen"]');
      if (seen) {
        seen.setAttribute('data-ts', agent.last_seen || '');
        if (!prefs.relativeTime) {
          seen.textContent = agent.last_seen || '-';
        }
      }
      row.setAttribute('data-status', agent.status);
    }

    function reapplyFilters() {
      var filterRoot = table.closest('[data-filter-root]');
      var filterText = filterRoot ? filterRoot.querySelector('[data-filter-text]') : null;
      if (filterText) {
        filterText.dispatchEvent(new Event('input'));
      }
      renderRelativeTimes();
    }

    function fetchAgents() {
      return fetch(url, { credentials: 'same-origin' }).then(function (response) {
        if (!response.ok) throw new Error('Agents refresh failed');
        return response.json();
      });
    }

    function refresh() {
      if (!prefs.liveAgents) {
        return;
      }
      fetchAgents()
        .then(function (items) {
          var byId = {};
          (items || []).forEach(function (agent) { byId[agent.agent_id] = agent; });
          table.querySelectorAll('tr[data-agent-id]').forEach(function (row) {
            var agent = byId[row.getAttribute('data-agent-id')];
            if (agent) {
              applyAgentRow(row, agent);
            }
          });
          reapplyFilters();
        })
        .catch(function () {});
    }

    table.addEventListener('click', function (event) {
      var button = event.target.closest('[data-agent-refresh]');
      if (!button || button.disabled) {
        return;
      }
      var row = button.closest('tr[data-agent-id]');
      if (!row) {
        return;
      }
      var agentId = row.getAttribute('data-agent-id');
      var label = button.textContent;
      button.disabled = true;
      button.textContent = 'Refreshing...';
      fetchAgents()
        .then(function (items) {
          var agent = (items || []).find(function (item) {
            return item.agent_id === agentId;
          });
          if (!agent) {
            throw new Error('Agent is no longer registered.');
          }
          applyAgentRow(row, agent);
          reapplyFilters();
        })
        .catch(function (error) {
          showFlash([{ category: 'danger', message: error.message || 'Could not refresh agent.' }]);
        })
        .then(function () {
          if (document.body.contains(button)) {
            button.textContent = label;
            button.disabled = false;
          }
        });
    });

    trackTimer(window.setInterval(refresh, 20000));
  }

  function initLiveTaskOutput() {
    var blocks = Array.prototype.slice.call(document.querySelectorAll('[data-live-task]'));
    if (!blocks.length) {
      return;
    }
    var POLL_MS = 1500;
    var FINAL_STATES = ['failed', 'killed', 'cancelled'];

    function setMeta(block, stream, text) {
      var meta = block.querySelector('[data-live-meta="' + stream + '"]');
      if (meta) {
        meta.textContent = (text ? text.length : 0) + ' chars';
      }
    }

    function stopPolling(block) {
      if (block.liveTimer) {
        window.clearInterval(block.liveTimer);
        block.liveTimer = null;
      }
    }

    function finalize(block, data) {
      var card = block.closest('[data-filter-row]');
      var statusPill = card ? card.querySelector('[data-task-status]') : null;
      if (statusPill) {
        statusPill.textContent = data.status;
        statusPill.className = 'pill pill-' + data.status;
      }
      if (card) {
        card.setAttribute('data-status', data.status);
        var killForm = card.querySelector('[data-task-kill]');
        if (killForm) {
          killForm.hidden = true;
        }
        var retryForm = card.querySelector('[data-task-retry]');
        if (retryForm) {
          retryForm.hidden = FINAL_STATES.indexOf(data.status) === -1;
        }
      }
      var state = block.querySelector('[data-live-state]');
      if (state) {
        state.hidden = true;
      }
      block.classList.remove('output-block-live');
      var root = block.closest('[data-filter-root]');
      if (root) {
        root.querySelectorAll('[data-filter-attr]').forEach(function (select) {
          select.dispatchEvent(new Event('change'));
        });
      }
    }

    function render(block, data) {
      var card = block.closest('[data-filter-row]');
      var stdoutPre = block.querySelector('[data-live-pre="stdout"]');
      var stderrWrap = block.querySelector('[data-live-error]');
      var stderrPre = block.querySelector('[data-live-pre="stderr"]');
      var state = block.querySelector('[data-live-state]');
      if (card) {
        var statusPill = card.querySelector('[data-task-status]');
        if (statusPill && statusPill.textContent.trim() !== data.status) {
          statusPill.textContent = data.status;
          statusPill.className = 'pill pill-' + data.status;
          card.setAttribute('data-status', data.status);
          card.setAttribute(
            'data-search',
            (card.getAttribute('data-search') || '') + ' ' + data.status
          );
          var root = card.closest('[data-filter-root]');
          if (root) {
            root.querySelectorAll('[data-filter-attr]').forEach(function (select) {
              select.dispatchEvent(new Event('change'));
            });
          }
        }
      }
      var stick = stdoutPre
        ? stdoutPre.scrollTop + stdoutPre.clientHeight >= stdoutPre.scrollHeight - 24
        : false;
      if (stdoutPre) {
        var text = data.stdout || '';
        if (!text && data.final) {
          text = '(no output captured)';
        } else if (!text && !data.final && data.status === 'pending') {
          text = 'Waiting for the agent...';
        }
        stdoutPre.textContent = text;
        if (stick) {
          stdoutPre.scrollTop = stdoutPre.scrollHeight;
        }
      }
      if (stderrWrap && stderrPre) {
        var errText = data.stderr || '';
        stderrWrap.hidden = !errText;
        stderrPre.textContent = errText;
      }
      setMeta(block, 'stdout', data.stdout);
      setMeta(block, 'stderr', data.stderr);
      if (state && !data.final) {
        state.textContent = data.status === 'running' ? 'Live' : 'Queued';
        state.classList.toggle('is-pending', data.status !== 'running');
        state.hidden = false;
      }
    }

    function poll(block) {
      if (document.hidden) {
        return;
      }
      var taskId = block.getAttribute('data-live-task');
      fetch('/api/tasks/' + encodeURIComponent(taskId) + '/live', { credentials: 'same-origin' })
        .then(function (response) {
          if (!response.ok) throw new Error('live output unavailable');
          return response.json();
        })
        .then(function (data) {
          render(block, data);
          if (data.final) {
            stopPolling(block);
            finalize(block, data);
          }
        })
        .catch(function () {
          var state = block.querySelector('[data-live-state]');
          if (state) {
            state.textContent = 'Reconnecting';
            state.classList.add('is-pending');
          }
        });
    }

    blocks.forEach(function (block) {
      poll(block);
      block.liveTimer = trackTimer(window.setInterval(function () { poll(block); }, POLL_MS));
    });
  }

  document.body.classList.toggle('compact', prefs.compact);
  bindCopyButtons();
  bindAsyncForms();
  initPageFeatures();
  window.setInterval(renderRelativeTimes, 30000);

  function markDirtyFromInput(event) {
    var target = event.target;
    if (!target || !target.closest) {
      return;
    }
    var form = target.closest('form');
    if (!form || form.closest('[data-no-warn]')) {
      return;
    }
    formState.dirty = true;
  }

  document.addEventListener('input', markDirtyFromInput, true);
  document.addEventListener('change', markDirtyFromInput, true);

  window.addEventListener('pagehide', function () {
    clearPageTimers();
    document.querySelectorAll('[data-live-task]').forEach(function (block) {
      if (block.liveTimer) {
        window.clearInterval(block.liveTimer);
        block.liveTimer = null;
      }
    });
  });

  window.addEventListener('beforeunload', function (event) {
    if (!formState.dirty) {
      return;
    }
    event.preventDefault();
    event.returnValue = '';
  });

  function toMB(bytes) {
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  }

  function setProgress(percent, label) {
    var bar = document.getElementById('upload-progress-bar');
    var text = document.getElementById('upload-progress-text');
    var percentEl = document.getElementById('upload-progress-percent');
    if (!bar || !text || !percentEl) {
      return;
    }
    var bounded = Math.max(0, Math.min(100, percent));
    bar.value = bounded;
    text.textContent = label || 'Uploading...';
    percentEl.textContent = Math.round(bounded) + '%';
  }

  function renderDebugSummary(summary) {
    var panel = document.getElementById('upload-debug-summary');
    if (!panel) {
      return;
    }
    var items = summary.items || [];
    var lines = [];
    var system = summary.system || {};
    lines.push('System summary:');
    lines.push('- agents: ' + (system.agents_count || 0));
    lines.push('- scripts: ' + (system.scripts_total || 0));
    lines.push('- tasks total: ' + (system.tasks_count || 0));
    lines.push('- tasks pending/running/failed: ' + (system.tasks_pending || 0) + '/' + (system.tasks_running || 0) + '/' + (system.tasks_failed || 0));
    lines.push('');
    lines.push('Upload limit: ' + toMB(summary.upload_limit_bytes || 0));
    lines.push('MAX_CONTENT_LENGTH: ' + toMB(summary.max_content_length || 0));
    lines.push('File scripts: ' + (summary.file_script_count || 0));
    lines.push('Base64 encoded: ' + (summary.base64_encoded_count || 0));
    lines.push('Stored bytes total: ' + toMB(summary.total_script_bytes || 0));
    lines.push('Server time: ' + (summary.server_time || '-'));
    lines.push('');
    lines.push('Recent uploaded scripts:');
    if (!items.length) {
      lines.push('- none');
    } else {
      items.slice(0, 12).forEach(function (item) {
        lines.push(
          '- ' + item.script_id + ' | ' + (item.file_name || '-') + ' | ' +
          toMB(item.content_size || 0) + ' | ' + (item.content_sha256 || '-')
        );
      });
    }
    panel.textContent = lines.join('\n');
  }

  function fetchDebugSummary() {
    var panel = document.getElementById('upload-debug-summary');
    if (!panel) {
      return;
    }
    fetch('/api/uploads/debug-summary', { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('Failed to fetch debug summary');
        }
        return response.json();
      })
      .then(renderDebugSummary)
      .catch(function (error) {
        panel.textContent = 'Debug summary error: ' + error.message;
      });
  }

  function initUploadPanel() {
    var refreshBtn = document.getElementById('refresh-debug-btn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', fetchDebugSummary);
      fetchDebugSummary();
    }

    var uploadForm = document.getElementById('upload-form');
    if (uploadForm) {
    uploadForm.addEventListener('submit', function (event) {
      event.preventDefault();
      var submitBtn = document.getElementById('upload-submit-btn');
      var fileInput = document.getElementById('script-upload-input');
      if (!fileInput || !fileInput.files || !fileInput.files.length) {
        setProgress(0, 'Please choose a file first.');
        return;
      }

      var formData = new FormData(uploadForm);
      var csrfMeta = document.querySelector('meta[name="csrf-token"]');
      if (csrfMeta && csrfMeta.content) {
        formData.append('csrf_token', csrfMeta.content);
      }
      var xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/uploads/script');
      xhr.withCredentials = true;
      xhr.timeout = 120000;
      xhr.setRequestHeader('X-CSRFToken', csrfMeta ? csrfMeta.content : '');

      if (submitBtn) {
        submitBtn.disabled = true;
      }
      setProgress(0, 'Starting upload...');

      xhr.upload.onprogress = function (event) {
        if (!event.lengthComputable) {
          setProgress(50, 'Uploading...');
          return;
        }
        var percent = (event.loaded / event.total) * 100;
        setProgress(percent, 'Uploading ' + toMB(event.loaded) + ' / ' + toMB(event.total));
      };

      xhr.onload = function () {
        if (submitBtn) {
          submitBtn.disabled = false;
        }
        var responseBody = {};
        try {
          responseBody = JSON.parse(xhr.responseText || '{}');
        } catch (parseError) {
          responseBody = { ok: false, error: xhr.statusText || 'Invalid server response' };
        }
        if (xhr.status >= 200 && xhr.status < 300 && responseBody.ok) {
          setProgress(100, 'Upload complete: ' + (responseBody.file_name || 'file'));
          uploadForm.reset();
          formState.dirty = false;
          fetchDebugSummary();
          softNavigate(window.location.href).then(function () {
            showFlash([{ category: 'success', message: 'Uploaded ' + (responseBody.file_name || 'script') + '.' }]);
          });
          return;
        }
        setProgress(0, 'Upload failed: ' + (responseBody.error || 'unknown error'));
      };

      xhr.onerror = function () {
        if (submitBtn) {
          submitBtn.disabled = false;
        }
        setProgress(0, 'Upload failed: network error');
      };

      xhr.ontimeout = function () {
        if (submitBtn) {
          submitBtn.disabled = false;
        }
        setProgress(0, 'Upload failed: request timed out');
      };

      xhr.send(formData);
    });
  }
  }

  function syncTaskForm() {
    var taskType = document.getElementById('task-type');
    var targetType = document.getElementById('target-type');
    var agentTarget = document.getElementById('agent-target');
    var groupTarget = document.getElementById('group-target');
    var scriptSelect = document.getElementById('script-select');
    var fileInput = document.querySelector('input[name="agent_file_name"]');
    var timeoutInput = document.querySelector('input[name="run_timeout_seconds"]');
    var nonzeroInput = document.querySelector('input[name="run_allow_nonzero_success"]');
    if (!taskType || !scriptSelect) {
      return;
    }
    if (targetType && agentTarget && groupTarget) {
      var isGroup = targetType.value === 'group';
      agentTarget.disabled = isGroup;
      groupTarget.disabled = !isGroup;
      agentTarget.required = !isGroup;
      groupTarget.required = isGroup;
    }
    var isAgentFile = taskType.value === 'run_agent_file';
    var isDeploy = taskType.value === 'deploy_script';
    scriptSelect.disabled = isAgentFile;
    scriptSelect.required = !isAgentFile;
    Array.prototype.forEach.call(scriptSelect.options, function (option) {
      option.disabled = isDeploy && option.getAttribute('data-script-kind') !== 'file';
    });
    if (scriptSelect.selectedOptions[0] && scriptSelect.selectedOptions[0].disabled) {
      for (var optionIndex = 0; optionIndex < scriptSelect.options.length; optionIndex += 1) {
        if (!scriptSelect.options[optionIndex].disabled) {
          scriptSelect.selectedIndex = optionIndex;
          break;
        }
      }
    }
    if (fileInput) {
      fileInput.disabled = !isAgentFile;
    }
    if (timeoutInput) {
      timeoutInput.disabled = !isAgentFile;
    }
    if (nonzeroInput) {
      nonzeroInput.disabled = !isAgentFile;
    }
  }

  function initTaskFormControls() {
    var taskType = document.getElementById('task-type');
    var targetType = document.getElementById('target-type');
    if (taskType) {
      taskType.addEventListener('change', syncTaskForm);
      syncTaskForm();
    }
    if (targetType) {
      targetType.addEventListener('change', syncTaskForm);
    }
  }

  function syncScheduleTarget() {
    var scheduleType = document.getElementById('schedule-target-type');
    var scheduleTarget = document.getElementById('schedule-target-id');
    if (!scheduleType || !scheduleTarget) {
      return;
    }
    var wanted = scheduleType.value === 'group' ? 'group' : 'agent';
    Array.prototype.forEach.call(scheduleTarget.options, function (option) {
      option.disabled = (option.getAttribute('data-target-kind') || 'agent') !== wanted;
    });
    var selected = scheduleTarget.selectedOptions[0];
    if (!selected || selected.disabled) {
      for (var index = 0; index < scheduleTarget.options.length; index += 1) {
        if (!scheduleTarget.options[index].disabled) {
          scheduleTarget.selectedIndex = index;
          break;
        }
      }
    }
  }

  function initScheduleFormControls() {
    var scheduleType = document.getElementById('schedule-target-type');
    if (scheduleType) {
      scheduleType.addEventListener('change', syncScheduleTarget);
      syncScheduleTarget();
    }
  }

  function formatHealthPercent(value) {
    return value === null || value === undefined ? '-' : Number(value).toFixed(1) + '%';
  }

  function setHealthField(container, field, value) {
    var element = container.querySelector('[data-health-field="' + field + '"]');
    if (element) {
      element.textContent = value;
    }
  }

  function updateHealthPanel(panel, payload) {
    var agentId = panel.getAttribute('data-agent-id');
    var agents = payload.agents || [];
    if (agentId) {
      var agent = agents.filter(function (item) { return item.agent_id === agentId; })[0];
      if (!agent) {
        return;
      }
      setHealthField(panel, 'cpu', formatHealthPercent(agent.cpu_percent));
      setHealthField(panel, 'memory', formatHealthPercent(agent.memory_percent));
      setHealthField(panel, 'disk', formatHealthPercent(agent.disk_percent));
      setHealthField(panel, 'reported', 'Updated ' + (agent.reported_at || '-'));
      var updated = document.getElementById('agent-health-updated');
      if (updated) {
        updated.textContent = 'Updated ' + (payload.updated_at || '-');
      }
      return;
    }

    var rows = panel.querySelectorAll('[data-agent-id]');
    agents.forEach(function (agent) {
      var row = null;
      Array.prototype.forEach.call(rows, function (candidate) {
        if (candidate.getAttribute('data-agent-id') === agent.agent_id) {
          row = candidate;
        }
      });
      if (!row) {
        return;
      }
      setHealthField(row, 'status', agent.status);
      setHealthField(row, 'cpu', formatHealthPercent(agent.cpu_percent));
      setHealthField(row, 'memory', formatHealthPercent(agent.memory_percent));
      setHealthField(row, 'disk', formatHealthPercent(agent.disk_percent));
      setHealthField(row, 'reported', agent.reported_at || '-');
      setHealthField(row, 'specs', (agent.architecture || '-') + ' · ' + (agent.logical_cpu_count || '-') + ' CPUs');
      var status = row.querySelector('[data-health-field="status"]');
      if (status) {
        status.className = 'pill pill-' + agent.status;
      }
    });
    var agentCount = document.getElementById('health-agent-count');
    var alertCount = document.getElementById('health-alert-count');
    var updated = document.getElementById('health-last-updated');
    if (agentCount) agentCount.textContent = agents.length;
    if (alertCount) alertCount.textContent = payload.open_alert_count || 0;
    if (updated) updated.textContent = 'Updated ' + (payload.updated_at || '-');
  }

  function refreshHealthPanels() {
    var panels = document.querySelectorAll('[data-health-url]');
    Array.prototype.forEach.call(panels, function (panel) {
      var url = panel.getAttribute('data-health-url');
      if (panel.getAttribute('data-agent-id')) {
        url += '?agent_id=' + encodeURIComponent(panel.getAttribute('data-agent-id'));
      }
      fetch(url, { credentials: 'same-origin' })
        .then(function (response) {
          if (!response.ok) throw new Error('Health update failed');
          return response.json();
        })
        .then(function (payload) { updateHealthPanel(panel, payload); })
        .catch(function () {});
    });
  }

  function initHealthPanels() {
    if (!document.querySelector('[data-health-url]')) {
      return;
    }
    refreshHealthPanels();
    trackTimer(window.setInterval(refreshHealthPanels, 5000));
  }

  function initAgentDownloadForm() {
    var form = document.getElementById('agentForm');
    var link = document.getElementById('agentDownload');
    if (!form || !link) {
      return;
    }
    var baseHref = (link.getAttribute('href') || '/agent/download').split('?')[0];
    function sync() {
      var url = document.getElementById('agent-server-url');
      var poll = document.getElementById('agent-poll');
      var ssl = document.getElementById('agent-ssl');
      var params = new URLSearchParams();
      if (url && url.value.trim()) {
        params.set('url', url.value.trim());
      }
      if (poll && poll.value) {
        params.set('poll', poll.value);
      }
      params.set('ssl', ssl && ssl.checked ? '1' : '0');
      link.href = baseHref + '?' + params.toString();
    }
    form.addEventListener('input', sync);
    form.addEventListener('change', sync);
    sync();
  }

  function initPageFeatures() {
    clearPageTimers();
    document.querySelectorAll('[data-live-task]').forEach(function (block) {
      if (block.liveTimer) {
        window.clearInterval(block.liveTimer);
        block.liveTimer = null;
      }
    });
    document.body.classList.toggle('compact', prefs.compact);
    bindPreferenceControls();
    updatePreferenceControls();
    initFilters();
    initLiveAgents();
    initLiveTaskOutput();
    initAgentDownloadForm();
    initUploadPanel();
    initTaskFormControls();
    initScheduleFormControls();
    initHealthPanels();
    renderRelativeTimes();
  }

  function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  }

  function isLoginTarget(url) {
    try {
      var parsed = new URL(url, window.location.href);
      return parsed.origin === window.location.origin && parsed.pathname === '/login';
    } catch (error) {
      return false;
    }
  }

  function showFlash(messages) {
    var items = (messages || []).filter(function (item) {
      return item && item.message;
    });
    if (!items.length) {
      return;
    }
    var stack = document.getElementById('flash-stack');
    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'flash-stack';
      stack.className = 'flash-stack';
      document.body.appendChild(stack);
    }
    items.forEach(function (item) {
      while (stack.children.length >= 4) {
        stack.removeChild(stack.firstElementChild);
      }
      var alert = document.createElement('div');
      alert.className = 'alert alert-' + (item.category || 'info');
      alert.textContent = item.message;
      stack.appendChild(alert);
      var lifetime = item.category === 'danger' || item.category === 'warning' ? 8000 : 4500;
      window.setTimeout(function () {
        alert.classList.add('is-leaving');
        window.setTimeout(function () {
          alert.remove();
        }, 250);
      }, lifetime);
    });
  }

  function softNavigate(url, options) {
    var opts = options || {};
    return fetch(url, {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch' }
    }).then(function (response) {
      if (!response.ok) {
        throw new Error('Could not load page (' + response.status + ').');
      }
      var finalUrl = response.url || url;
      return response.text().then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var newMain = doc.querySelector('main.page-wrap');
        var currentMain = document.querySelector('main.page-wrap');
        if (!doc.querySelector('.app-shell') || !newMain || !currentMain) {
          window.location.href = finalUrl;
          return false;
        }
        var samePage = finalUrl.split('#')[0] === window.location.href.split('#')[0];
        var scrollY = window.scrollY;
        clearPageTimers();
        currentMain.innerHTML = newMain.innerHTML;
        var newNav = doc.querySelector('.side-nav');
        var nav = document.querySelector('.side-nav');
        if (newNav && nav) {
          nav.innerHTML = newNav.innerHTML;
        }
        var newFoot = doc.querySelector('.sidebar-foot');
        var foot = document.querySelector('.sidebar-foot');
        if (newFoot && foot) {
          foot.innerHTML = newFoot.innerHTML;
        }
        var newTitle = doc.querySelector('.topbar-title');
        var title = document.querySelector('.topbar-title');
        if (newTitle && title) {
          title.textContent = newTitle.textContent;
        }
        document.title = doc.title;
        formState.dirty = false;
        initPageFeatures();
        if (samePage) {
          window.scrollTo(0, scrollY);
        } else {
          window.scrollTo(0, 0);
          if (opts.push !== false) {
            window.history.pushState({ soft: true }, '', finalUrl);
          }
        }
        return true;
      });
    });
  }

  function submitFormAsync(form) {
    var buttons = Array.prototype.slice.call(
      form.querySelectorAll('button[type="submit"], button:not([type])')
    );
    var previousStates = buttons.map(function (button) {
      return button.disabled;
    });
    buttons.forEach(function (button) {
      button.disabled = true;
    });
    form.classList.add('is-busy');
    formState.dirty = false;

    var body = new FormData(form);
    var csrf = getCsrfToken();
    if (csrf) {
      body.set('csrf_token', csrf);
    }

    var action = form.getAttribute('action') || window.location.pathname;
    fetch(new URL(action, document.baseURI).href, {
      method: 'POST',
      body: body,
      credentials: 'same-origin',
      headers: {
        'X-Requested-With': 'fetch',
        'X-CSRFToken': csrf,
        'Accept': 'application/json'
      }
    })
      .then(function (response) {
        var contentType = response.headers.get('Content-Type') || '';
        if (contentType.indexOf('application/json') === -1) {
          window.location.href = response.url || window.location.href;
          return null;
        }
        return response.json().then(function (payload) {
          if (!response.ok || payload.ok === false) {
            throw new Error(payload.error || 'Request failed with status ' + response.status + '.');
          }
          var target = payload.redirect || window.location.href;
          if (isLoginTarget(target)) {
            window.location.href = target;
            return null;
          }
          return softNavigate(target).then(function () {
            showFlash(payload.messages);
          });
        });
      })
      .catch(function (error) {
        showFlash([{ category: 'danger', message: error.message || 'Request failed.' }]);
      })
      .then(function () {
        if (document.body.contains(form)) {
          form.classList.remove('is-busy');
          buttons.forEach(function (button, index) {
            button.disabled = previousStates[index];
          });
        }
      });
  }

  function bindAsyncForms() {
    document.addEventListener('submit', function (event) {
      var form = event.target;
      if (!form || form.tagName !== 'FORM') {
        return;
      }
      if (event.defaultPrevented || form.hasAttribute('data-no-async') || form.id === 'upload-form') {
        return;
      }
      var method = (form.getAttribute('method') || 'get').toLowerCase();
      if (method === 'get') {
        if (!form.hasAttribute('data-async-get')) {
          return;
        }
        event.preventDefault();
        var params = new URLSearchParams();
        new FormData(form).forEach(function (value, key) {
          params.append(key, value);
        });
        var base = form.getAttribute('action') || window.location.pathname;
        var query = params.toString();
        formState.dirty = false;
        softNavigate(base + (query ? '?' + query : '')).catch(function (error) {
          showFlash([{ category: 'danger', message: error.message }]);
        });
        return;
      }
      if (method !== 'post') {
        return;
      }
      event.preventDefault();
      submitFormAsync(form);
    });
  }

  window.addEventListener('popstate', function () {
    softNavigate(window.location.href, { push: false }).catch(function () {
      window.location.reload();
    });
  });
})();
