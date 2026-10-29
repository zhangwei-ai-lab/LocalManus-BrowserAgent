let currentEventSource = null;
let aiMessageDiv = null;
let finalAnswer = null;
let thoughtQuote = null;
let chatMessages = null;
let chat_state = 'none';
let activeTaskId = null;
let taskWatchdog = null;

function getMarkedText(text) {
    return DOMPurify.sanitize(marked.parse(text))
}

function showToast(message, kind) {
    const toastEl = document.getElementById('errorToast');
    const header = toastEl.querySelector('.toast-header');
    // 同一个 toast 复用两种语义：错误（红）/ 提示（蓝）
    header.className = 'toast-header ' + (kind === 'info' ? 'bg-primary' : 'bg-danger') + ' text-white';
    toastEl.querySelector('.toast-body').textContent = message;

    const toast = new bootstrap.Toast(toastEl);
    toast.show();
}

function showErrorToast(message) {
    showToast(message, 'error');
}

function showInfoToast(message) {
    showToast(message, 'info');
}

function toggle_chat_state(state) {
    chat_state = state
    const spinner = document.getElementById('send-spinner');
    if (spinner) spinner.style.display = chat_state;
    // 忙的时候才露出「重置」按钮：给用户一条退出通道
    const resetBtn = document.getElementById('btnReset');
    if (resetBtn) resetBtn.classList.toggle('d-none', chat_state === 'none');
}

/**
 * 唯一的收尾出口。
 *
 * 旧实现里所有失败分支都只弹一个 toast 就走人（建任务请求失败、重连耗尽……），
 * chat_state 仍停在 working 上，于是下一句话会被 sendMessage 拦下来，
 * 弹出 “Chat bot still under working, please wait...”，彻底卡死到刷新页面为止。
 */
function endChat() {
    toggle_chat_state('none');
    activeTaskId = null;
    stopTaskWatchdog();
}

function stopTaskWatchdog() {
    if (taskWatchdog !== null) {
        clearInterval(taskWatchdog);
        taskWatchdog = null;
    }
}

/** 从 /tasks/{id} 的 steps 里取最终答复（complete 事件丢了也能补上）。 */
function extractResult(task) {
    if (!task || !Array.isArray(task.steps)) return '';
    for (let i = task.steps.length - 1; i >= 0; i--) {
        const step = task.steps[i];
        if (step && step.type === 'result' && step.result) return step.result;
    }
    return '';
}

/**
 * 用后端状态给前端「对账」。
 *
 * SSE 断线、标签页被挂起、服务重启都会让前端以为任务还在跑，而服务端其实早就结束了。
 * 返回 true = 任务确实还在运行；false = 已结束/已丢失，状态也已复位。
 */
function reconcileTask(taskId) {
    return fetch(`/tasks/${taskId}`)
        .then(response => {
            if (response.status === 404) {
                // 服务重启过，内存里的任务没了，这次对话只能放弃
                endChat();
                showInfoToast('服务端已重启，上一个任务不存在了，输入框已恢复');
                return false;
            }
            if (!response.ok) return true;
            return response.json().then(task => {
                const status = String(task.status || '');
                if (status === 'completed') {
                    const result = extractResult(task);
                    if (result) addMessage(result, 'ai');
                    endChat();
                    return false;
                }
                if (status.indexOf('failed') === 0) {
                    endChat();
                    showErrorToast(status);
                    return false;
                }
                return true;
            });
        })
        .catch(() => true); // 服务不可达：先不改状态，交给看门狗继续试
}

/** 任务期间每 10 秒对一次账，SSE 漏事件时也能自动解锁界面。 */
function startTaskWatchdog(taskId) {
    stopTaskWatchdog();
    const startedAt = Date.now();
    let longRunWarned = false;

    taskWatchdog = setInterval(() => {
        if (chat_state === 'none' || activeTaskId !== taskId) {
            stopTaskWatchdog();
            return;
        }
        reconcileTask(taskId).then(stillRunning => {
            if (!stillRunning) return;
            if (!longRunWarned && Date.now() - startedAt > 20 * 60 * 1000) {
                longRunWarned = true;
                showInfoToast('任务已运行 20 分钟仍未结束，可查看后台日志或点「重置」释放界面');
            }
        });
    }, 10000);
}

function createChat() {
    const isLongThought = document.getElementById('longThoughtCheckbox').checked;
    const promptInput = document.getElementById('messageInput');
    const prompt = promptInput.value.trim();

    if (!prompt) {
        endChat(); // 空输入也不能把状态留在 working 上
        showErrorToast("Please enter a valid prompt");
        promptInput.focus();
        return;
    }

    if (currentEventSource) {
        currentEventSource.close();
        currentEventSource = null;
    }

    fetch('/tasks', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ prompt })
    })
        .then(response => {
            if (!response.ok) {
                return response.json().then(err => { throw new Error(err.detail || 'Request failed') });
            }
            return response.json();
        })
        .then(data => {
            if (!data.task_id) {
                throw new Error('Invalid task ID');
            }
            activeTaskId = data.task_id;
            addMessage(prompt, 'user');
            setupSSE(data.task_id, isLongThought);
            startTaskWatchdog(data.task_id);
            promptInput.value = '';
        })
        .catch(error => {
            // 关键修复：请求失败必须复位，否则下一条消息永远发不出去
            endChat();
            showErrorToast(error.message);
            console.error('Failed to create task:', error);
        });
}

function setupSSE(taskId, isLongThought) {
    let retryCount = 0;
    const maxRetries = 3;
    const retryDelay = 2000;
    let lastResultContent = '';

    function connect() {
        const eventSource = new EventSource(`/tasks/${taskId}/events`);
        currentEventSource = eventSource;

        const handleEvent = (event, type) => {
            try {
                const data = JSON.parse(event.data);
                if (!isLongThought) {
                    if (type === 'act') {
                        addMessage(data.result, 'ai')
                    }
                    return;
                }

                if (type === 'log' && data.result.indexOf('Executing step') > -1) {
                    createLongThought(data.result);
                } else if (type === 'act') {
                    finalAnswer.textContent = data.result
                } else {
                    const stepDiv = document.createElement('div');
                    stepDiv.className = 'thinking-message';
                    stepDiv.textContent = data.result;
                    thoughtQuote.querySelector('.quote-content').appendChild(stepDiv);
                }
            } catch (e) {
                console.error(`Error handling ${type} event:`, e);
            }
        };

        const eventTypes = ['think', 'tool', 'act', 'log', 'run', 'message'];
        eventTypes.forEach(type => {
            eventSource.addEventListener(type, (event) => handleEvent(event, type));
        });

        eventSource.addEventListener('complete', (event) => {
            try {
                const data = JSON.parse(event.data);
                lastResultContent = data.result || '';

                if (lastResultContent) {
                    if (isLongThought) {
                        finalAnswer.innerHTML = getMarkedText(lastResultContent);
                    } else {
                        addMessage(lastResultContent, 'ai');
                    }
                    // console.log(lastResultContent);
                }
                scrollView();
                eventSource.close();
                currentEventSource = null;
            } catch (e) {
                console.error('Error handling complete event:', e);
            }
            endChat();
        });

        eventSource.addEventListener('error', (event) => {
            try {
                console.error(event)
                const data = JSON.parse(event.data);
                showErrorToast(data.message)
                eventSource.close();
                currentEventSource = null;
            } catch (e) {
                console.error('Error handling failed:', e);
            }
            endChat();
        });

        eventSource.onerror = (err) => {
            if (eventSource.readyState === EventSource.CLOSED) return;

            console.error('SSE connection error:', err);
            eventSource.close();
            currentEventSource = null;

            // 断线不等于失败：先问后端这个任务到底怎么样了
            reconcileTask(taskId).then(stillRunning => {
                if (!stillRunning) return; // 已结束/已丢失，reconcile 里已复位
                if (retryCount < maxRetries) {
                    retryCount++;
                    showErrorToast(`Connection lost, retrying in ${retryDelay / 1000} seconds (${retryCount}/${maxRetries})`)
                    setTimeout(connect, retryDelay);
                } else {
                    // 关键修复：重连耗尽也要复位，不能把用户锁死在 working 状态
                    endChat();
                    showErrorToast('Connection lost, input restored. Please try again or refresh the page.');
                }
            });
        };
    }

    connect();
}

function loadHistory() {
    fetch('/tasks')
        .then(response => {
            if (!response.ok) {
                return response.text().then(text => {
                    throw new Error(`request failure: ${response.status} - ${text.substring(0, 100)}`);
                });
            }
            return response.json();
        })
        .then(tasks => {
            applyHistory(tasks)
        })
        .catch(error => {
            console.error('Failed to load history records:', error);
            showErrorToast(error.message)
        });
}

function applyHistory(tasks) {
    if (!tasks) return;
    const historyModal = new bootstrap.Modal(document.getElementById('historyModal'));
    const historyList = document.getElementById('historyList');

    historyList.innerHTML = '';

    if (tasks.length === 0) {
        historyList.innerHTML = '<li class="list-group-item text-muted">Record not found!</li>';
    } else {
        tasks.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
        tasks.forEach(item => {
            const title = item.prompt
            const li = document.createElement('li');
            li.className = 'list-group-item d-flex justify-content-between align-items-center';
            li.innerHTML = `
                <div class="fw-bold">${title}</div>
                <small class="text-muted">${new Date(item.created_at).toLocaleString()}</small>
            `;

            li.addEventListener('click', function () {
                chatMessages.innerHTML = '';
                addMessage(item.prompt, 'user');
                item.steps.forEach(step => {
                    if (step.type === 'result') {
                        return;
                    }
                    if (step.type === 'log' && step.result.indexOf('Executing step') > -1) {
                        createLongThought(step.result);
                    } else if (step.type === 'act') {
                        finalAnswer.textContent = step.result
                    } else {
                        const stepDiv = document.createElement('div');
                        stepDiv.className = 'thinking-message';
                        stepDiv.textContent = step.result;
                        thoughtQuote.querySelector('.quote-content').appendChild(stepDiv);
                    }
                });

                historyModal.hide();
            });

            historyList.appendChild(li);
        });
    }

    historyModal.show();
}

function addMessage(text, sender) {
    const messageDiv = document.createElement('div');
    messageDiv.classList.add(sender + '-message');

    const iconDiv = document.createElement('div');
    iconDiv.className = 'message-icon';
    const icon = document.createElement('i');
    icon.className = sender === 'user' ? 'bi bi-person-fill' : 'bi bi-robot';
    iconDiv.appendChild(icon);

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    if (sender === 'user') {
        contentDiv.innerHTML = `
        <div class="user-prompt">${text}</div>
        <button class="copy-btn" onclick="copyMessage(this)" title="Copy">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"></rect><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path></svg>
        </button>
        `;
    } else {
        contentDiv.textContent = text;
    }

    if (sender === 'user') {
        messageDiv.appendChild(contentDiv);
        messageDiv.appendChild(iconDiv);
    } else {
        messageDiv.appendChild(iconDiv);
        messageDiv.appendChild(contentDiv);
    }

    chatMessages.appendChild(messageDiv);
    scrollView();
}

function scrollView() {
    if (!chatMessages) return
    chatMessages.scrollIntoView({ behavior: "auto", block: "end" })
}

function createLongThought(prompt) {
    aiMessageDiv = document.createElement('div');
    aiMessageDiv.className = 'message ai-message';

    const iconDiv = document.createElement('div');
    iconDiv.className = 'message-icon';
    const icon = document.createElement('i');
    icon.className = 'bi bi-robot';
    iconDiv.appendChild(icon);

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';

    thoughtQuote = document.createElement('div');
    thoughtQuote.className = 'thought-quote';
    thoughtQuote.innerHTML = `
        <div class="quote-header">
            <span>Thinking...</span>
            <span class="toggle-icon expanded"><i class="bi bi-chevron-down"></i></span>
        </div>
        <div class="quote-content">
            <div class="thinking-message">${getMarkedText(prompt)}</div>
        </div>
    `;

    finalAnswer = document.createElement('div');
    finalAnswer.className = 'ai-final-answer';

    contentDiv.appendChild(thoughtQuote);
    contentDiv.appendChild(finalAnswer);

    aiMessageDiv.appendChild(iconDiv);
    aiMessageDiv.appendChild(contentDiv);

    chatMessages.appendChild(aiMessageDiv);
    scrollView();

    thoughtQuote.addEventListener('click', function (e) {
        if (e.target.closest('.quote-header')) {
            const isCollapsing = !this.classList.contains('collapsed');
            this.classList.toggle('collapsed');
            const icon = this.querySelector('.toggle-icon');
            icon.innerHTML = isCollapsing ? '<i class="bi bi-chevron-up"></i>' : '<i class="bi bi-chevron-down"></i>';
        }
    });
}

function getCustomCss() {
    return `
        <style>
            .message-content h1, .message-content h2, .message-content h3,
            .message-content h4, .message-content h5, .message-content h6 {
                margin: 10px 0;
                color: #343a40;
            }
            .message-content p {
                margin: 5px 0;
            }
            .message-content ul, .message-content ol {
                margin: 10px 0;
                padding-left: 20px;
            }
            .message-content li {
                margin: 5px 0;
            }
            .message-content a {
                color: #007bff;
                text-decoration: none;
            }
            .message-content a:hover {
                text-decoration: underline;
            }
            .message-content code {
                background-color: #f8f9fa;
                padding: 2px 4px;
                border-radius: 4px;
            }
            .message-content pre {
                background-color: #f8f9fa;
                padding: 10px;
                border-radius: 8px;
                overflow-x: auto;
            }
        </style>
    `;
}

document.addEventListener('DOMContentLoaded', function () {
    // Initialize tooltip
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.forEach(tooltipTriggerEl => {
        new bootstrap.Tooltip(tooltipTriggerEl);
    });

    document.getElementById('btn-paperclip').addEventListener('click', function() {
        document.getElementById('fileInput').click();
    });

    document.getElementById('fileInput').addEventListener('change', function(event) {
        const fileInput = event.target;
        const file = event.target.files[0];
        if (file) {
            if (file.type === 'text/plain') {
                const reader = new FileReader();
                reader.onload = function(e) {
                    const content = e.target.result;
                    document.getElementById('messageInput').value = content;
                    fileInput.value = '';
                };
                reader.readAsText(file);
            } else {
                showErrorToast('Please select a text (.txt) file.');
                document.getElementById('fileInput').value = '';
                document.getElementById('messageInput').value = '';
                fileInput.value = '';
            }
        }
    });

    const messageInput = document.getElementById('messageInput');
    const sendButton = document.getElementById('sendButton');
    chatMessages = document.getElementById('chatMessages');

    if (!messageInput || !sendButton || !chatMessages) {
        console.error('Required elements not found!');
        return;
    }

    toggle_chat_state('none');

    const promptShortcuts = document.querySelectorAll('.prompt-shortcut');

    async function sendMessage() {
        if (chat_state !== 'none') {
            // 不要一上来就拦住用户：先跟后端对一次账，很可能任务早就结束了
            const stillRunning = activeTaskId ? await reconcileTask(activeTaskId) : false;
            if (stillRunning) {
                showInfoToast('上一个任务还在运行，等它结束或点「重置」再发下一条');
                return;
            }
            // 对账没得出结论但状态还卡着：兜底复位，绝不让输入框永久锁死
            if (chat_state !== 'none') endChat();
        }
        const message = messageInput.value.trim();
        if (message) {
            toggle_chat_state('');
            createChat();
        }
    }

    sendButton.addEventListener('click', sendMessage);
    messageInput.addEventListener('keypress', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    promptShortcuts.forEach(shortcut => {
        shortcut.addEventListener('click', function () {
            messageInput.value = this.textContent;
            messageInput.focus();
        });
    });

    document.querySelector('.btn-chat').addEventListener('click', function () {
        chatMessages.innerHTML = '';
    });

    // 手动退出通道：任务卡住（模型不返回 / 后端失联）时释放界面
    const resetBtn = document.getElementById('btnReset');
    if (resetBtn) {
        resetBtn.addEventListener('click', function () {
            if (chat_state === 'none') return;
            const ok = window.confirm(
                '任务可能仍在后台运行，产物还会写进 workspace。\n确定要释放界面、开始新的一轮对话吗？'
            );
            if (!ok) return;
            if (currentEventSource) {
                currentEventSource.close();
                currentEventSource = null;
            }
            endChat();
            showInfoToast('已释放界面状态，可以继续发消息了');
        });
    }

    document.querySelector('.btn-history').addEventListener('click', function () {
        loadHistory();
    });
});
