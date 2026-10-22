// Shared behavior for the admin screens, which are only ever used on a
// desktop computer: Enter-to-submit forms with a default button, a
// consistent confirm dialog, and HTML escaping for rendered data.

// Customer-entered values (names, addresses, order notes) end up in admin
// tables built with innerHTML - escape them, or a customer could plant
// markup that runs in the admin's logged-in session.
export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Enter anywhere inside a form presses its default (type="submit") button.
// Chrome already does this natively from every field type, but implicit
// submission from checkboxes, radios, and selects isn't consistent across
// browsers - handle it once for the whole page so it doesn't depend on
// which browser the admin uses. Textareas keep Enter for newlines; buttons
// keep Enter for themselves.
export function enableEnterToSubmit() {
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.isComposing || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    const el = e.target;
    const isField =
      el instanceof HTMLSelectElement ||
      (el instanceof HTMLInputElement && !["button", "submit", "reset", "file"].includes(el.type));
    if (!isField || !el.form) return;
    e.preventDefault();
    el.form.requestSubmit();
  });
}

// Wires a form's submit (button click or Enter) to an async action. The
// default button is disabled while it runs, so a double Enter can't send
// the same create twice. A thrown error is shown in the form's .error
// element; the action itself shows its own success toast.
export function handleSubmit(form, action) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (form.dataset.busy) return;
    const submitButton = form.querySelector('button[type="submit"]');
    const errorEl = form.querySelector(".error");
    if (errorEl) errorEl.style.display = "none";
    form.dataset.busy = "true";
    if (submitButton) submitButton.disabled = true;
    try {
      await action();
    } catch (err) {
      if (errorEl) {
        errorEl.textContent = err.message;
        errorEl.style.display = "block";
      }
    } finally {
      delete form.dataset.busy;
      if (submitButton) submitButton.disabled = false;
    }
  });
}

// A styled stand-in for window.confirm(). The confirm button is the
// dialog's default - focused on open, so Enter confirms; Escape or Cancel
// backs out. Resolves true only when confirmed.
export function confirmAction(message, { confirmLabel = "Delete", cancelLabel = "Cancel" } = {}) {
  let dialog = document.getElementById("confirm-dialog");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "confirm-dialog";
    dialog.innerHTML = `
      <form method="dialog">
        <h2>Please confirm</h2>
        <p id="confirm-dialog-message"></p>
        <div class="dialog-actions">
          <button type="submit" class="secondary" value="cancel" id="confirm-dialog-cancel-button">Cancel</button>
          <button type="submit" value="ok" id="confirm-dialog-ok-button">Delete</button>
        </div>
      </form>`;
    document.body.appendChild(dialog);
  }
  dialog.querySelector("#confirm-dialog-message").textContent = message;
  const okButton = dialog.querySelector("#confirm-dialog-ok-button");
  okButton.textContent = confirmLabel;
  dialog.querySelector("#confirm-dialog-cancel-button").textContent = cancelLabel;
  dialog.returnValue = "";

  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.showModal();
    okButton.focus();
  });
}
