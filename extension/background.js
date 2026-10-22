// background.js

chrome.runtime.onMessageExternal.addListener((request, sender, sendResponse) => {
  if (request.action === "OPEN_AND_AUTOFILL") {
    const targetUrl = request.url;
    const jobId = request.jobId;

    chrome.tabs.create({ url: targetUrl }, (tab) => {
      // Store current active autofill session
      chrome.storage.local.set({ activeAutofill: { tabId: tab.id, jobId: jobId } });
      sendResponse({ status: "TAB_OPENED", tabId: tab.id });
    });
    return true; // Keep message channel open for async response
  }
});

// Notify content script when the page is fully loaded
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete") {
    chrome.storage.local.get(["activeAutofill"], (res) => {
      if (res.activeAutofill && res.activeAutofill.tabId === tabId) {
        chrome.tabs.sendMessage(tabId, { action: "TRIGGER_FILL" });
      }
    });
  }
});