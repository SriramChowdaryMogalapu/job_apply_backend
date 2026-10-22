// content.js
const API_BASE = "http://127.0.0.1:8000";

// Comprehensive field matching dictionary
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

function normalizeText(text) {
  return (text || "").replace(/[^a-zA-Z0-9\s]/g, "").toLowerCase().trim();
}

function getFieldIdentifier(el) {
  const labelEl = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
  const labelText = labelEl ? labelEl.innerText : "";
  const ariaLabel = el.getAttribute("aria-label") || "";
  const placeholder = el.getAttribute("placeholder") || "";
  const name = el.getAttribute("name") || "";
  const id = el.getAttribute("id") || "";

  return `${name} ${id} ${ariaLabel} ${placeholder} ${labelText}`.toLowerCase();
}

function triggerDomEvents(el, val) {
  el.focus();
  el.value = val;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.blur();
  el.style.outline = "2px solid #10b981"; // Emerald green border for visual confirmation
}

async function executeAutofill() {
  console.log("[JobApply] Triggering adaptive autofill...");

  // 1. Fetch live profile and learned knowledge base from FastAPI
  let payload;
  try {
    const res = await fetch(`${API_BASE}/api/autofill-payload`);
    payload = await res.json();
  } catch (err) {
    console.error("[JobApply] Backend unavailable:", err);
    return;
  }

  const profile = payload.profile || {};
  const knowledgeBase = payload.knowledge_base || [];

  const inputs = Array.from(document.querySelectorAll("input:not([type='hidden']), textarea, select"));

  inputs.forEach((input) => {
    const idStr = getFieldIdentifier(input);
    let matched = false;

    // A. Match standard profile fields
    for (const [key, regexList] of Object.entries(FIELD_RULES)) {
      if (regexList.some((rgx) => rgx.test(idStr))) {
        const val = profile[key];
        if (val !== undefined && val !== null && String(val).trim() !== "") {
          triggerDomEvents(input, String(val));
          input.dataset.knownField = "true";
          matched = true;
          break;
        }
      }
    }

    // B. Match adaptive knowledge base (previously learned questions)
    if (!matched && knowledgeBase.length > 0) {
      for (const entry of knowledgeBase) {
        const normKey = entry.question_key.replace(/_/g, " ");
        if (idStr.includes(normKey)) {
          triggerDomEvents(input, entry.stored_answer);
          input.dataset.knownField = "true";
          matched = true;
          break;
        }
      }
    }

    // C. Attach learning listener for unknown fields
    if (!input.dataset.listenerAttached) {
      input.dataset.listenerAttached = "true";
      input.addEventListener("change", async (e) => {
        const newVal = e.target.value.trim();
        // If user fills an unknown field or overrides a value, learn it
        if (newVal && newVal.length > 1) {
          const rawPrompt = idStr.slice(0, 120);
          console.log(`[JobApply] Learning new field: "${rawPrompt}" -> "${newVal}"`);
          try {
            await fetch(`${API_BASE}/api/knowledge-base/learn`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                raw_question: rawPrompt,
                answer: newVal,
                field_type: input.tagName.toLowerCase()
              })
            });
          } catch (err) {
            console.error("[JobApply] Failed to persist learned field:", err);
          }
        }
      });
    }
  });
}

// Listen for trigger command from background.js
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "TRIGGER_FILL") {
    setTimeout(executeAutofill, 800); // 800ms grace period for dynamic React/Vue ATS hydration
    sendResponse({ status: "STARTED" });
  }
});