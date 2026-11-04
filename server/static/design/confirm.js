/* Design Uniformity: inline confirm for plain HTML forms (spec 7.10), with no framework.
 *
 *   <form method="post" data-confirm="Reboot the Pi? Its screens go dark for a minute.">
 *     <button>Reboot</button>
 *   </form>
 *
 * Submitting the form opens the popover next to the button that was pressed, instead of
 * window.confirm(). The yes button says what the submit button says, or data-confirm-yes.
 * The attribute can also sit on one button (<button data-confirm="…">) to ask for that one only.
 * Load it once per page: <script src="/static/design/confirm.js" defer></script>
 */
(function () {
  var open = null;

  function close() {
    if (!open) return;
    open.pop.remove();
    open.button.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("click", onOutside, true);
    open.button.focus();
    open = null;
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  function onOutside(e) { if (open && !open.wrap.contains(e.target)) close(); }

  function ask(form, button, question) {
    close();
    var wrap = button.parentNode;
    if (!wrap.classList || !wrap.classList.contains("confirm-wrap")) {
      wrap = document.createElement("span");
      wrap.className = "confirm-wrap";
      button.parentNode.insertBefore(wrap, button);
      wrap.appendChild(button);
    }
    var left = button.getBoundingClientRect().left < 320;   // near the left edge: open rightwards
    var pop = document.createElement("span");
    pop.className = "confirm" + (left ? " left" : "");
    pop.setAttribute("role", "dialog");
    pop.innerHTML = '<span style="display:block"></span><span class="confirm-actions">' +
      '<button type="button" class="btn btn-ghost">Cancel</button><button type="button" class="btn btn-danger"></button></span>';
    pop.firstChild.textContent = question;
    var buttons = pop.querySelectorAll("button");
    buttons[1].textContent = button.dataset.confirmYes || form.dataset.confirmYes || (button.textContent || "").trim() || "Yes";
    buttons[0].addEventListener("click", close);
    buttons[1].addEventListener("click", function () {
      close();
      form.dataset.confirmed = "1";
      if (form.requestSubmit) form.requestSubmit(button.form === form && button.type === "submit" ? button : undefined);
      else form.submit();
    });
    wrap.appendChild(pop);
    button.setAttribute("aria-expanded", "true");
    open = { pop: pop, button: button, wrap: wrap };
    document.addEventListener("keydown", onKey);
    document.addEventListener("click", onOutside, true);
    buttons[1].focus();
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    var button = e.submitter || form.querySelector("button[type=submit], button:not([type]), input[type=submit]");
    var question = (button && button.dataset.confirm) || form.dataset.confirm;
    if (!question) return;
    if (form.dataset.confirmed) { delete form.dataset.confirmed; return; }
    e.preventDefault();
    if (button) ask(form, button, question);
  }, true);
})();
