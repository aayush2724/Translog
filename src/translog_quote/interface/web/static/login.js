"use strict";

/* Sign-in behaviour. The CSP forbids inline script, so this lives in its own
   file served from the same origin. It POSTs the dashboard access key as JSON
   (the server's cross-site guard requires application/json), lets the server
   set the HttpOnly session cookie, and on success navigates to the dashboard.
   The key is the whole secret — there is no username — and this never stores it
   nor reads the cookie; that is the server's.

   Accessibility: the error strip is a live region; when it shows, the field is
   marked invalid and described by the error as well as the hint, and focus
   returns to the field so a screen-reader user hears what went wrong and is
   already placed to fix it. A typed key is never cleared on failure. */

(function () {
  const form = document.getElementById("login-form");
  const field = document.getElementById("login-password");
  const errorEl = document.getElementById("login-error");
  const button = document.getElementById("login-submit");

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", "login-error login-hint");
    field.focus();
  }

  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = "";
    field.removeAttribute("aria-invalid");
    field.setAttribute("aria-describedby", "login-hint");
  }

  field.addEventListener("input", function () {
    if (!errorEl.hidden) clearError();
  });

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (button.disabled) return; /* a double submit while one is in flight */
    clearError();

    const password = field.value;
    if (!password.trim()) {
      /* Nothing to send: say so here rather than round-tripping an empty key. */
      showError("Enter the dashboard access key.");
      return;
    }

    button.disabled = true;
    button.textContent = "Signing in…";
    try {
      const response = await fetch("/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (response.ok) {
        /* The server has set the session cookie; go to the desk. */
        window.location.assign("/");
        return;
      }
      const payload = await response.json().catch(() => ({}));
      if (response.status === 429) {
        const wait = Number(payload.retry_after) || 60;
        showError(
          "Too many sign-in attempts. Wait about " + wait + " seconds and try again.",
        );
      } else if (payload.error === "invalid credentials") {
        showError("That access key was not recognised. Check it and try again.");
      } else {
        showError("Sign-in failed. Please try again.");
      }
    } catch (err) {
      showError("The server did not respond. Check your connection and try again.");
    }
    button.disabled = false;
    button.textContent = "Sign in";
  });
})();
