// popup.js
const FRONTEND_PROFILE_URL = "https://jobapplyfrontend.vercel.app/profile";
const consoleBox = document.getElementById("console");

function appendLog(message, type = "info") {
  const line = document.createElement("div");
  line.className = `log-line log-${type}`;
  const timestamp = new Date().toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  line.textContent = `[${timestamp}] ${message}`;
  consoleBox.appendChild(line);
  consoleBox.scrollTop = consoleBox.scrollHeight;
}

// Clear log button
document.getElementById("clearLogs").addEventListener("click", () => {
  consoleBox.innerHTML = '<div class="log-line log-info">Ready to autofill.</div>';
});

// Listen for live log streams broadcast by content.js
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.action === "LOG_EVENT") {
    appendLog(msg.text, msg.logType || "info");
  }
});

document.getElementById("autofillBtn").addEventListener("click", async () => {
  appendLog("Starting autofill scan...", "info");

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.id) {
      appendLog("Error: No active tab found.", "error");
      return;
    }

    try {
      await chrome.tabs.sendMessage(tab.id, { action: "TRIGGER_FILL" });
    } catch {
      appendLog("Injecting script into tab...", "info");
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        files: ["content.js"]
      });

      setTimeout(async () => {
        try {
          await chrome.tabs.sendMessage(tab.id, { action: "TRIGGER_FILL" });
        } catch {
          appendLog("Could not connect to current tab.", "error");
        }
      }, 300);
    }
  } catch (err) {
    appendLog(`Error: ${err.message}`, "error");
  }
});

document.getElementById("profileBtn").addEventListener("click", () => {
  chrome.tabs.create({ url: FRONTEND_PROFILE_URL });
});