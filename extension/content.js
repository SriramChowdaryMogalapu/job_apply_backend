// content.js
const API_BASE = "https://job-apply-backend-hl5v.onrender.com";

const FIELD_RULES = {
  first_name: [/\bfirst[_\s]?name\b/i, /\bgiven[_\s]?name\b/i, /\bfname\b/i],
  middle_name: [/\bmiddle[_\s]?name\b/i],
  last_name: [/\blast[_\s]?name\b/i, /\bsurname\b/i, /\bfamily[_\s]?name\b/i, /\blname\b/i],
  full_name: [/\bfull[_\s]?name\b/i, /^name$/i],
  email: [/\be-?mail\b/i],
  phone: [/\bphone\b/i, /\bmobile\b/i, /\bcontact[_\s]?no\b/i, /\bcell\b/i],
  phone_code: [/\bcountry[_\s]?code\b/i, /\bphone[_\s]?code\b/i, /\bdialing[_\s]?code\b/i],
  address_line1: [/\baddress\b/i, /\bstreet\b/i, /\bline[_\s]?1\b/i],
  city: [/\bcity\b/i, /\btown\b/i],
  state: [/\bstate\b/i, /\bprovince\b/i, /\bregion\b/i],
  postal_code: [/\bzip\b/i, /\bpostal\b/i, /\bpincode\b/i],
  country: [/\bcountry\b/i],
  current_company: [/\bcompany\b/i, /\borganization\b/i, /\bemployer\b/i],
  current_role: [/\btitle\b/i, /\bcurrent[_\s]?role\b/i, /\bdesignation\b/i],
  linkedin_url: [/linkedin/i],
  github_url: [/github/i],
  portfolio_url: [/portfolio/i, /website/i, /personal[_\s]?site/i],
  notice_period_days: [/notice[_\s]?period/i],
  current_ctc: [/current[_\s]?(ctc|salary|compensation)/i],
  expected_ctc: [/expected[_\s]?(ctc|salary|compensation)/i],
  years_of_experience: [/years?(\s*of)?\s*experience/i, /\btotal\s*exp/i]
};

// ----------------- 1. LOG DISPATCHER TO POPUP -----------------
function emitLog(text, logType = "info") {
  try {
    chrome.runtime.sendMessage({ action: "LOG_EVENT", text, logType }).catch(() => {});
  } catch {
    // Popup might not be open; ignore gracefully
  }
}

// ----------------- 2. TOP-RIGHT SHADOW DOM TOAST -----------------
let toastShadowRoot = null;

function getToastContainer() {
  if (toastShadowRoot) {
    const existing = toastShadowRoot.getElementById("toast-wrapper");
    if (existing) return existing;
  }

  let host = document.getElementById("jobapply-toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "jobapply-toast-host";
    host.style.all = "initial";
    (document.documentElement || document.body).appendChild(host);
  }

  toastShadowRoot = host.attachShadow({ mode: "open" });

  const style = document.createElement("style");
  style.textContent = `
    #toast-wrapper {
      position: fixed !important;
      top: 24px !important;
      right: 24px !important;
      display: flex !important;
      flex-direction: column !important;
      gap: 10px !important;
      z-index: 2147483647 !important;
      pointer-events: none !important;
      max-width: 360px !important;
    }
    .toast-item {
      pointer-events: auto !important;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif !important;
      font-size: 13px !important;
      font-weight: 600 !important;
      line-height: 1.4 !important;
      padding: 12px 18px !important;
      border-radius: 8px !important;
      color: #ffffff !important;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25) !important;
      display: flex !important;
      align-items: center !important;
      gap: 10px !important;
      opacity: 0;
      transform: translateY(-12px);
      transition: opacity 0.25s ease, transform 0.25s ease !important;
    }
    .toast-item.visible {
      opacity: 1 !important;
      transform: translateY(0) !important;
    }
    .toast-info { background: #0f172a !important; border-left: 4px solid #38bdf8 !important; }
    .toast-success { background: #0f172a !important; border-left: 4px solid #10b981 !important; }
    .toast-learned { background: #064e3b !important; border-left: 4px solid #34d399 !important; }
    .toast-error { background: #450a0a !important; border-left: 4px solid #f87171 !important; }
  `;
  toastShadowRoot.appendChild(style);

  const wrapper = document.createElement("div");
  wrapper.id = "toast-wrapper";
  toastShadowRoot.appendChild(wrapper);

  return wrapper;
}

function showToast(message, type = "info") {
  emitLog(message, type);

  try {
    const container = getToastContainer();
    const toast = document.createElement("div");
    toast.className = `toast-item toast-${type}`;
    toast.textContent = message;

    container.appendChild(toast);

    requestAnimationFrame(() => {
      toast.classList.add("visible");
    });

    setTimeout(() => {
      toast.classList.remove("visible");
      setTimeout(() => toast.remove(), 250);
    }, 4000);
  } catch (e) {
    console.error("[JobApply Toast Error]", e);
  }
}

// ----------------- 3. DOM & EVENT DISPATCHERS -----------------
function setNativeValue(element, value) {
  const valueSetter = Object.getOwnPropertyDescriptor(element, "value")?.set;
  const prototype = Object.getPrototypeOf(element);
  const prototypeValueSetter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;

  if (prototypeValueSetter && valueSetter !== prototypeValueSetter) {
    prototypeValueSetter.call(element, value);
  } else if (valueSetter) {
    valueSetter.call(element, value);
  } else {
    element.value = value;
  }
}

function triggerInputEvents(el, val) {
  el.focus();
  setNativeValue(el, val);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true }));
  el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
  el.blur();
}

function getFieldIdentifier(el) {
  const labelEl = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
  const labelText = labelEl ? labelEl.innerText : "";
  const ariaLabel = el.getAttribute("aria-label") || "";
  const ariaLabelledBy = el.getAttribute("aria-labelledby")
    ? document.getElementById(el.getAttribute("aria-labelledby"))?.innerText || ""
    : "";
  const placeholder = el.getAttribute("placeholder") || "";
  const name = el.getAttribute("name") || "";
  const id = el.getAttribute("id") || "";
  const dataTestId = el.getAttribute("data-testid") || "";

  return `${name} ${id} ${dataTestId} ${ariaLabel} ${ariaLabelledBy} ${placeholder} ${labelText}`.toLowerCase();
}

function cleanString(str) {
  return String(str || "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function handleSelectElement(selectEl, targetValue) {
  if (!targetValue) return false;

  const options = Array.from(selectEl.options).filter((o) => o.value && o.text.trim());
  if (options.length === 0) return false;

  const cleanTarget = cleanString(targetValue);

  const exactMatch = options.find((o) => cleanString(o.text) === cleanTarget || cleanString(o.value) === cleanTarget);
  if (exactMatch) {
    selectEl.value = exactMatch.value;
    selectEl.dispatchEvent(new Event("change", { bubbles: true }));
    emitLog(`Selected option: "${exactMatch.text}"`, "success");
    return true;
  }

  const filteredMatches = options.filter((o) => {
    const oText = cleanString(o.text);
    const oVal = cleanString(o.value);
    return oText.includes(cleanTarget) || cleanTarget.includes(oText) || oVal.includes(cleanTarget);
  });

  if (filteredMatches.length === 1) {
    selectEl.value = filteredMatches[0].value;
    selectEl.dispatchEvent(new Event("change", { bubbles: true }));
    emitLog(`Fuzzy selected: "${filteredMatches[0].text}"`, "success");
    return true;
  }

  return false;
}

// ----------------- 4. MAIN AUTOFILL ROUTINE -----------------
async function executeAutofill() {
  showToast("Contacting backend...", "info");

  let payload;
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 75000);

    const res = await fetch(`${API_BASE}/api/autofill-payload`, { signal: controller.signal });
    clearTimeout(timeoutId);

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
    emitLog("Profile & KB payload loaded.", "info");
  } catch (err) {
    console.error("[JobApply] Backend fetch failed:", err);
    showToast("Backend unreachable. Please wait if waking up.", "error");
    return;
  }

  const profile = payload.profile || {};
  const knowledgeBase = payload.knowledge_base || [];

  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden']), textarea, select"));
  emitLog(`Found ${inputs.length} form controls on page.`, "info");
  let filledCount = 0;

  inputs.forEach((element) => {
    const idStr = getFieldIdentifier(element);
    let targetValue = null;
    let matchedName = null;

    for (const [key, regexList] of Object.entries(FIELD_RULES)) {
      if (regexList.some((rgx) => rgx.test(idStr))) {
        targetValue = profile[key];
        matchedName = key;
        break;
      }
    }

    if (!targetValue && knowledgeBase.length > 0) {
      for (const entry of knowledgeBase) {
        const normKey = entry.question_key.replace(/_/g, " ");
        if (idStr.includes(normKey)) {
          targetValue = entry.stored_answer;
          matchedName = `KB:${entry.question_key}`;
          break;
        }
      }
    }

    if (element.tagName.toLowerCase() === "select") {
      if (handleSelectElement(element, targetValue)) {
        filledCount++;
      }
    } else {
      if (targetValue !== undefined && targetValue !== null && String(targetValue).trim() !== "") {
        triggerInputEvents(element, String(targetValue));
        element.dataset.knownField = "true";
        emitLog(`Filled [${matchedName}]`, "success");
        filledCount++;
      }
    }

    // Adaptive Learning
    if (!element.dataset.listenerAttached) {
      element.dataset.listenerAttached = "true";
      element.addEventListener("change", async (e) => {
        const newVal = e.target.value?.trim();
        if (newVal && newVal.length > 0) {
          const rawPrompt = idStr.slice(0, 120);
          try {
            await fetch(`${API_BASE}/api/knowledge-base/learn`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                raw_question: rawPrompt,
                answer: newVal,
                field_type: element.tagName.toLowerCase()
              })
            });

            showToast("Saved answer to knowledge base!", "learned");
          } catch (err) {
            console.error("[JobApply] Failed to persist learned field:", err);
          }
        }
      });
    }
  });

  if (filledCount > 0) {
    showToast(`Autofilled ${filledCount} field${filledCount > 1 ? "s" : ""}!`, "success");
  } else {
    showToast("Scanned page, but found no matching fields to fill.", "info");
  }
}

// ----------------- 5. MESSAGE LISTENER -----------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "TRIGGER_FILL") {
    executeAutofill();
    sendResponse({ status: "SUCCESS" });
  }
});