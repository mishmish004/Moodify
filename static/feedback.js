// Phase 14: likes and ratings without a page reload. The forms work without this script too: they post
// and the server redirects back. Here the same post asks for JSON instead, and the page updates in place.
const toast = document.getElementById("toast");
let hideToast;

function say(text) {
  clearTimeout(hideToast);
  toast.textContent = text || "";
  if (text) hideToast = setTimeout(() => { toast.textContent = ""; }, 10000);
}

for (const form of document.querySelectorAll("form[data-feedback]")) {
  form.addEventListener("submit", async (event) => {
    if (form.dataset.plain) return;  // the fallback below: let the browser post it
    event.preventDefault();
    const button = event.submitter;
    const body = new URLSearchParams(new FormData(form));
    if (button && button.name) body.set(button.name, button.value);
    button.disabled = true;
    let answer = null;
    try {
      const resp = await fetch(form.action, { method: "POST", body, headers: { Accept: "application/json" } });
      if (resp.ok && (resp.headers.get("content-type") || "").startsWith("application/json")) answer = await resp.json();
    } catch (error) { /* offline or similar: handled below */ }
    button.disabled = false;
    if (!answer) {
      // Logged out, not found, offline: post it the plain way, so the server's own page explains
      form.dataset.plain = "1";
      form.requestSubmit(button);
      return;
    }
    if (form.dataset.feedback === "like") {
      button.setAttribute("aria-pressed", String(answer.liked));
      form.action = form.action.replace(/\/(un)?like$/, answer.liked ? "/unlike" : "/like");
    } else {
      for (const b of form.querySelectorAll("button")) b.setAttribute("aria-pressed", String(Number(b.value) === answer.rating));
    }
    say(answer.notice);
  });
}
