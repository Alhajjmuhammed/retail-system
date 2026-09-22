// The "are you sure?" dialog, shared by every destructive button.
//
// Lives in a file rather than inline in the template because the site sends
// a Content-Security-Policy that does not allow inline script -- and when it
// was inline, the store was never defined in production and every delete
// button threw "$store.confirm is undefined" into the console and did
// nothing at all.
document.addEventListener("alpine:init", () => {
  Alpine.store("confirm", {
    open: false,
    title: "",
    body: "",
    label: "Confirm",
    danger: false,
    form: null,

    ask(form, options) {
      this.form = form;
      this.title = options.title || "Are you sure?";
      this.body = options.body || "";
      this.label = options.label || "Confirm";
      this.danger = options.danger !== false;
      this.open = true;
    },

    cancel() {
      this.open = false;
      this.form = null;
    },

    proceed() {
      const form = this.form;
      this.open = false;
      this.form = null;
      if (!form) return;
      // An HTMX form that swaps in place (hx-trigger="confirmed") is sent
      // by HTMX; a native submit would leave the modal for a whole page.
      if (window.htmx && form.getAttribute("hx-trigger") === "confirmed") {
        window.htmx.trigger(form, "confirmed");
        return;
      }
      // Submit past the @submit.prevent that opened this, rather than
      // calling submit() and re-triggering the handler.
      form.submit();
    },
  });
});
