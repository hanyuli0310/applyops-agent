document.addEventListener('DOMContentLoaded', () => {
    // --- Elements ---
    const connDot = document.getElementById('conn-dot');
    const connText = document.getElementById('conn-text');
    const connPill = document.getElementById('conn-pill');
    
    const jobUrlInput = document.getElementById('job-url');
    const pasteBtn = document.getElementById('paste-btn');
    const applyBtn = document.getElementById('apply-btn');
    const applyBtnLabel = document.getElementById('apply-btn-label');
    const applySpinner = document.getElementById('apply-spinner');
    
    const agentPulse = document.getElementById('agent-pulse');
    const agentStateText = document.getElementById('agent-state-text');
    const stopBtn = document.getElementById('stop-btn');
    const actionFeed = document.getElementById('action-status-feed');
    
    const viewportBox = document.getElementById('viewport-box');
    const viewportEmpty = document.getElementById('viewport-empty');
    const viewportImg = document.getElementById('viewport-img');
    
    const askModal = document.getElementById('ask-modal');
    const askQuestionText = document.getElementById('ask-question-text');
    const askContextText = document.getElementById('ask-context-text');
    const askAnswerInput = document.getElementById('ask-answer-input');
    const askSubmitBtn = document.getElementById('ask-submit-btn');
    const askSuggestionBox = document.getElementById('ask-suggestion-box');
    const askSuggestionText = document.getElementById('ask-suggestion-text');
    const askSuggestionConf = document.getElementById('ask-suggestion-conf');
    const askUseSuggestionBtn = document.getElementById('ask-use-suggestion-btn');
    
    const flywheelRate = document.getElementById('flywheel-rate');
    const ringArc = document.getElementById('ring-arc');
    const ringPct = document.getElementById('ring-pct');
    const mMemories = document.getElementById('m-memories');
    const mAutoReady = document.getElementById('m-autoready');
    const mAsked = document.getElementById('m-asked');
    const mSelectors = document.getElementById('m-selectors');
    const flywheelPlatforms = document.getElementById('flywheel-platforms');
    
    const waitModal = document.getElementById('wait-modal');
    const waitMessageText = document.getElementById('wait-message-text');
    const waitResumeBtn = document.getElementById('wait-resume-btn');
    
    const profileForm = document.getElementById('profile-form');
    const qaList = document.getElementById('qa-list');
    const qaCount = document.getElementById('qa-count');
    const qaSearch = document.getElementById('qa-search');
    
    const historyList = document.getElementById('history-list');
    const historyCount = document.getElementById('history-count');
    
    const settingsBtn = document.getElementById('settings-btn');
    const settingsModal = document.getElementById('settings-modal');
    const closeSettingsBtn = document.getElementById('close-settings-btn');
    const cancelSettingsBtn = document.getElementById('cancel-settings-btn');
    const settingsForm = document.getElementById('settings-form');
    
    // --- State ---
    let ws = null;
    let reconnectAttempts = 0;
    let currentAskId = null;
    let allQAItems = [];

    // --- WebSocket Logic ---
    function initWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            connDot.className = 'status-dot connected';
            connText.textContent = 'Agent Connected';
            reconnectAttempts = 0;
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleServerMessage(msg);
            } catch (err) {
                console.error("Failed to parse websocket message:", err);
            }
        };

        ws.onclose = () => {
            connDot.className = 'status-dot disconnected';
            connText.textContent = 'Disconnected';
            scheduleReconnect();
        };

        ws.onerror = (err) => {
            console.error("WebSocket error:", err);
            ws.close();
        };
    }

    function scheduleReconnect() {
        const delay = Math.min(1000 * Math.pow(1.5, reconnectAttempts), 15000);
        reconnectAttempts++;
        setTimeout(initWebSocket, delay);
    }

    function sendWs(type, payload = {}) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type, ...payload }));
        } else {
            showToast("Not connected to agent server", "error");
        }
    }

    function handleServerMessage(msg) {
        switch (msg.type) {
            case 'status':
                actionFeed.textContent = msg.message;
                break;

            case 'screenshot':
                if (msg.data) {
                    viewportEmpty.classList.add('hidden');
                    viewportImg.classList.remove('hidden');
                    viewportImg.src = `data:image/png;base64,${msg.data}`;
                }
                break;

            case 'ask':
                currentAskId = msg.id;
                askQuestionText.textContent = msg.question;
                askContextText.textContent = msg.context || "The application asks for this detail. Your answer will be remembered for future jobs.";
                askAnswerInput.value = "";
                // Never ask with an empty box — showing the best remembered guess
                // turns every interruption into a one-click confirmation.
                if (msg.suggestion) {
                    askSuggestionText.textContent = msg.suggestion;
                    askSuggestionConf.textContent = (msg.suggestion_confidence || 0).toFixed(2);
                    askSuggestionConf.className = 'conf-chip' + (msg.suggestion_confidence >= 0.6 ? '' : ' weak');
                    askSuggestionBox.classList.remove('hidden');
                } else {
                    askSuggestionBox.classList.add('hidden');
                }
                askModal.classList.remove('hidden');
                askAnswerInput.focus();
                break;

            case 'wait':
                waitMessageText.textContent = msg.reason || "Please resolve verification in the browser window and click continue.";
                waitModal.classList.remove('hidden');
                break;

            case 'learn':
                if (msg.auto) {
                    showToast(`Auto-answered from memory (${msg.confidence.toFixed(2)}): ${msg.answer}`, 'success');
                } else {
                    showToast(`Learned for next time: ${msg.answer}`, 'success');
                }
                loadQA();
                loadStats();
                break;

            case 'done':
                showToast(`Application Complete: ${msg.summary || msg.status}`, msg.status === 'success' ? 'success' : 'info');
                actionFeed.textContent = `Completed: ${msg.summary || msg.status}`;
                setAgentUIState('idle');
                loadHistory();
                loadQA();
                loadStats();
                break;

            case 'error':
                showToast(msg.message, 'error');
                actionFeed.textContent = `Error: ${msg.message}`;
                loadStats();
                break;

            case 'agent_state':
                setAgentUIState(msg.state);
                break;
        }
    }

    function setAgentUIState(state) {
        if (state === 'running') {
            agentPulse.classList.add('active');
            agentStateText.textContent = 'Agent Working...';
            applyBtn.disabled = true;
            applyBtnLabel.textContent = 'In Progress';
            applySpinner.classList.remove('hidden');
            stopBtn.classList.remove('hidden');
            askModal.classList.add('hidden');
            waitModal.classList.add('hidden');
        } else if (state === 'waiting_for_answer') {
            agentPulse.classList.add('active');
            agentStateText.textContent = 'Needs Candidate Answer';
        } else if (state === 'waiting_for_user') {
            agentPulse.classList.add('active');
            agentStateText.textContent = 'Human Intervention Required';
            waitModal.classList.remove('hidden');
        } else {
            // Idle
            agentPulse.classList.remove('active');
            agentStateText.textContent = 'Agent Idle';
            applyBtn.disabled = false;
            applyBtnLabel.textContent = 'Launch Agent';
            applySpinner.classList.add('hidden');
            stopBtn.classList.add('hidden');
            askModal.classList.add('hidden');
            waitModal.classList.add('hidden');
        }
    }

    // --- Action Handlers ---
    function launchAgent() {
        const url = jobUrlInput.value.trim();
        if (!url) {
            showToast("Please enter or paste a valid job link", "error");
            jobUrlInput.focus();
            return;
        }

        actionFeed.textContent = "Initializing job application cycle...";
        sendWs('apply', { url });
        setAgentUIState('running');
    }

    function submitAnswer() {
        const answer = askAnswerInput.value.trim();
        if (!answer) return;

        sendWs('answer', { id: currentAskId, answer });
        askModal.classList.add('hidden');
        actionFeed.textContent = `Answered & learned: "${answer}"`;
        setAgentUIState('running');
    }

    function resumeAfterWait() {
        sendWs('resume');
        waitModal.classList.add('hidden');
        actionFeed.textContent = "Resuming automated execution...";
        setAgentUIState('running');
    }

    function stopExecution() {
        sendWs('stop');
        setAgentUIState('idle');
        actionFeed.textContent = "Operation stopped by user.";
        showToast("Agent execution stopped", "info");
    }

    // --- REST APIs: Profile & Memory ---
    async function loadProfile() {
        try {
            const res = await fetch('/api/memory/profile');
            if (!res.ok) return;
            const data = await res.json();
            const prof = data.profile || {};
            
            if (prof.name) document.getElementById('p-name').value = prof.name;
            if (prof.email) document.getElementById('p-email').value = prof.email;
            if (prof.phone) document.getElementById('p-phone').value = prof.phone;
            if (prof.location) document.getElementById('p-location').value = prof.location;
            if (prof.resume_path) document.getElementById('p-resume').value = prof.resume_path;
            if (prof.work_authorization) document.getElementById('p-work-auth').value = prof.work_authorization;
            if (prof.years_of_experience) document.getElementById('p-yoe').value = prof.years_of_experience;
            if (prof.linkedin_url) document.getElementById('p-linkedin').value = prof.linkedin_url;
        } catch (e) {
            console.error("Failed to load profile:", e);
        }
    }

    async function saveProfile(e) {
        e.preventDefault();
        const formData = new FormData(profileForm);
        const profile = Object.fromEntries(formData.entries());

        try {
            const res = await fetch('/api/memory/profile', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ profile })
            });
            if (res.ok) {
                showToast("Candidate profile saved", "success");
            } else {
                throw new Error("Failed");
            }
        } catch (e) {
            showToast("Failed to save candidate profile", "error");
        }
    }

    async function loadQA() {
        try {
            const res = await fetch('/api/memory/qa');
            if (!res.ok) return;
            const data = await res.json();
            allQAItems = data.qa || [];
            qaCount.textContent = allQAItems.length;
            renderQA(allQAItems);
        } catch (e) {
            console.error("Failed to load Q&A memory:", e);
        }
    }

    function renderQA(items) {
        if (!items || items.length === 0) {
            qaList.innerHTML = `<div class="empty-list">No learned question-answer pairs yet. The agent will ask you when needed and remember your answers automatically!</div>`;
            return;
        }

        qaList.innerHTML = items.map(item => {
            const conf = item.confidence || 0;
            const level = conf >= 0.6 ? 'high' : (conf >= 0.35 ? 'mid' : 'low');
            const label = item.auto_ready ? 'auto-fills' : 'needs review';
            return `
            <div class="qa-card" id="qa-${item.id}">
                <div class="qa-q">${escapeHtml(item.question)}</div>
                <div class="qa-a">${escapeHtml(item.answer)}</div>
                <div class="qa-meta">
                    <span>Used ${item.times_used || 0}× · ${item.success_count || 0}✓ ${item.failure_count || 0}✗</span>
                    <span class="qa-confidence ${level}" title="Confidence ${conf.toFixed(2)}">${conf.toFixed(2)} ${label}</span>
                    <button class="qa-del-btn" onclick="window.deleteQAEntry('${item.id}')" title="Delete entry">✕ Remove</button>
                </div>
                <div class="qa-feedback">
                    <button class="fb-btn good" onclick="window.qaFeedback('${item.id}', true)">👍 Still correct</button>
                    <button class="fb-btn bad" onclick="window.qaFeedback('${item.id}', false)">👎 Wrong</button>
                </div>
            </div>
        `;
        }).join('');
    }

    window.qaFeedback = async (id, ok) => {
        try {
            const res = await fetch(`/api/memory/qa/${id}/feedback`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ success: ok })
            });
            if (res.ok) {
                showToast(ok ? "Marked correct — trust increased" : "Marked wrong — trust decreased", "info");
                loadQA();
                loadStats();
            }
        } catch (e) {
            showToast("Failed to record feedback", "error");
        }
    };

    async function loadStats() {
        try {
            const res = await fetch('/api/memory/stats');
            if (!res.ok) return;
            const s = await res.json();
            const pct = Math.round((s.automation_rate || 0) * 100);
            ringPct.textContent = `${pct}%`;
            const circumference = 2 * Math.PI * 52;
            ringArc.style.strokeDasharray = `${circumference}`;
            ringArc.style.strokeDashoffset = `${circumference * (1 - (s.automation_rate || 0))}`;
            flywheelRate.textContent = `${s.questions_automated}/${s.questions_encountered} questions answered without asking`;
            mMemories.textContent = s.qa_total;
            mAutoReady.textContent = s.qa_auto_ready;
            mAsked.textContent = s.questions_asked;
            mSelectors.textContent = `${Math.round((s.selector_hit_rate || 0) * 100)}%`;
            flywheelPlatforms.innerHTML = (s.platforms || []).map(p => {
                if (!p.runs) return `<span class="plat-chip">${escapeHtml(p.platform)} · untried</span>`;
                return `<span class="plat-chip"><b>${escapeHtml(p.platform)}</b> ${p.runs} runs · ${Math.round(p.success_rate * 100)}% ok · ${p.selectors_validated}/${p.selectors_total} selectors proven</span>`;
            }).join('');
        } catch (e) {
            console.error("Failed to load flywheel stats:", e);
        }
    }

    window.deleteQAEntry = async (id) => {
        try {
            const res = await fetch(`/api/memory/qa/${id}`, { method: 'DELETE' });
            if (res.ok) {
                showToast("Learned memory removed", "info");
                loadQA();
            }
        } catch (e) {
            showToast("Failed to delete memory item", "error");
        }
    };

    async function loadHistory() {
        try {
            const res = await fetch('/api/history');
            if (!res.ok) return;
            const data = await res.json();
            const history = data.history || [];
            historyCount.textContent = `${history.length} recorded`;

            if (history.length === 0) {
                historyList.innerHTML = `<div class="empty-list">No job applications submitted yet.</div>`;
                return;
            }

            historyList.innerHTML = history.slice().reverse().map(item => {
                const dateStr = item.applied_at ? new Date(item.applied_at).toLocaleDateString(undefined, {
                    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
                }) : '';
                return `
                    <div class="history-row">
                        <div class="history-info">
                            <h4>${escapeHtml(item.job_title || 'Job Application')}</h4>
                            <div class="history-meta">${escapeHtml(item.platform || 'Direct')} • ${dateStr}</div>
                        </div>
                        <span class="badge-status ${item.status || 'success'}">${escapeHtml(item.status || 'applied')}</span>
                    </div>
                `;
            }).join('');
        } catch (e) {
            console.error("Failed to load history:", e);
        }
    }

    // --- Settings Modal ---
    async function loadSettings() {
        try {
            const res = await fetch('/api/settings');
            if (!res.ok) return;
            const s = await res.json();
            if (s.llm_provider) document.getElementById('cfg-provider').value = s.llm_provider;
            if (s.llm_model) document.getElementById('cfg-model').value = s.llm_model;
            if (s.max_actions_per_job) document.getElementById('cfg-max-actions').value = s.max_actions_per_job;
            document.getElementById('cfg-headless').checked = !!s.headless;
        } catch (e) {
            console.error("Failed to load settings:", e);
        }
    }

    async function saveSettings(e) {
        e.preventDefault();
        const body = {
            llm_provider: document.getElementById('cfg-provider').value,
            llm_model: document.getElementById('cfg-model').value.trim(),
            max_actions_per_job: parseInt(document.getElementById('cfg-max-actions').value, 10) || 50,
            headless: document.getElementById('cfg-headless').checked
        };

        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            if (res.ok) {
                showToast("Settings updated", "success");
                settingsModal.classList.add('hidden');
            }
        } catch (e) {
            showToast("Failed to save settings", "error");
        }
    }

    // --- Utilities ---
    function showToast(text, type = 'info') {
        const shelf = document.getElementById('toast-shelf');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = text;
        shelf.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(50px)';
            toast.style.transition = 'all 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }, 3200);
    }

    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // --- Tab Switching ---
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
            const target = btn.getAttribute('data-tab');
            document.getElementById(target).classList.add('active');
        });
    });

    // --- Search QA ---
    if (qaSearch) {
        qaSearch.addEventListener('input', (e) => {
            const q = e.target.value.toLowerCase().trim();
            if (!q) {
                renderQA(allQAItems);
                return;
            }
            const filtered = allQAItems.filter(item => 
                (item.question && item.question.toLowerCase().includes(q)) ||
                (item.answer && item.answer.toLowerCase().includes(q))
            );
            renderQA(filtered);
        });
    }

    // --- Event Listeners ---
    applyBtn.addEventListener('click', launchAgent);
    jobUrlInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') launchAgent();
    });

    pasteBtn.addEventListener('click', async () => {
        try {
            const text = await navigator.clipboard.readText();
            jobUrlInput.value = text;
            showToast("Pasted job link", "info");
        } catch (err) {
            showToast("Could not access clipboard", "error");
        }
    });

    askSubmitBtn.addEventListener('click', submitAnswer);
    askAnswerInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') submitAnswer();
    });

    askUseSuggestionBtn.addEventListener('click', () => {
        askAnswerInput.value = askSuggestionText.textContent;
        askAnswerInput.focus();
    });

    waitResumeBtn.addEventListener('click', resumeAfterWait);
    stopBtn.addEventListener('click', stopExecution);

    profileForm.addEventListener('submit', saveProfile);
    settingsForm.addEventListener('submit', saveSettings);

    settingsBtn.addEventListener('click', () => {
        loadSettings();
        settingsModal.classList.remove('hidden');
    });

    closeSettingsBtn.addEventListener('click', () => settingsModal.classList.add('hidden'));
    cancelSettingsBtn.addEventListener('click', () => settingsModal.classList.add('hidden'));

    // --- Initialize ---
    initWebSocket();
    loadProfile();
    loadQA();
    loadHistory();
    loadStats();
});
