// background.js

// 1. External message listener from your Next.js frontend web button
chrome.runtime.onMessageExternal.addListener((request, sender, sendResponse) => {
  if (request.action === "OPEN_AND_AUTOFILL") {
    const targetUrl = request.url;
    const jobId = request.jobId;

    chrome.tabs.create({ url: targetUrl }, (tab) => {
      chrome.storage.local.set({ activeAutofill: { tabId: tab.id, jobId: jobId } });
      sendResponse({ status: "TAB_OPENED", tabId: tab.id });
    });
    return true;
  }
});

// 2. Auto-trigger when a tab created by the frontend completes loading
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete") {
    chrome.storage.local.get(["activeAutofill"], (res) => {
      if (res.activeAutofill && res.activeAutofill.tabId === tabId) {
        setTimeout(() => {
          chrome.tabs.sendMessage(tabId, { action: "TRIGGER_FILL" }).catch(() => {});
        }, 1500);
      }
    });
  }
});