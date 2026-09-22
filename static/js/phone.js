/*
 * Selling from a phone. The basket survives a reload (saved per shop and
 * person); the camera uses the browser's own barcode reader where there is
 * one, and typing the code where there is not.
 */
(function () {
  "use strict";

  function csrf() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : "";
  }

  window.phoneSale = function (config) {
    const key = "phone-basket-" + config.tenant_id + "-" + config.user_id;
    return {
      config: config,
      lines: [],
      query: "",
      results: [],
      status: "",
      scanning: false,
      scanHint: "",
      code: "",
      stream: null,
      detector: null,
      paying: false,
      method: "",
      reference: "",
      error: "",
      busy: false,
      done: null,
      saleId: null,
      // Who is buying, if anyone: prices come from their list, and they can
      // pay on account.
      customer: null,
      picking: false,
      customerQuery: "",
      customerResults: [],

      init() {
        try {
          const saved = JSON.parse(localStorage.getItem(key) || "null");
          if (saved) {
            this.lines = saved.lines || [];
            this.saleId = saved.saleId || null;
            this.customer = saved.customer || null;
          }
        } catch (e) { /* a fresh basket */ }
        this.$watch("lines", () => this.save());
        this.register();
      },

      async register() {
        // Listed once, so the shop can see this phone and switch it off.
        const key = "phone-registered-" + config.tenant_id;
        try {
          if (localStorage.getItem(key)) return;
          let id = localStorage.getItem("phone-device-id-" + config.tenant_id);
          if (!id) {
            id = "phone-" + crypto.randomUUID();
            localStorage.setItem("phone-device-id-" + config.tenant_id, id);
          }
          const r = await fetch("/api/v1/sync/devices/", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
            body: JSON.stringify({ device_id: id, kind: "phone", label: "Phone" }),
          });
          if (r.ok) localStorage.setItem(key, "1");
        } catch (e) { /* offline or no storage: try again next time */ }
      },

      save() {
        try {
          localStorage.setItem(key, JSON.stringify({ lines: this.lines, saleId: this.saleId,
                                                     customer: this.customer }));
        } catch (e) { /* private window: the basket just is not kept */ }
      },

      money(value) {
        return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(Math.round(value || 0));
      },

      // Rounded line by line, the way the server records it.
      coin(value) {
        const step = (this.config && this.config.money_step) || 0.01;
        return Math.round((value || 0) / step) * step;
      },

      get total() {
        return this.lines.reduce((sum, l) => sum + this.coin(l.qty * l.price), 0);
      },

      get count() {
        return this.lines.reduce((sum, l) => sum + l.qty, 0);
      },

      get canPay() {
        // On account counts as a way to pay, even where no cash or mobile
        // method is allowed on phones.
        return this.config.may_pay && this.methods.length > 0 &&
          (!this.config.pay_limit || this.total <= this.config.pay_limit);
      },

      customerParam() {
        return this.customer ? "&customer=" + this.customer.id : "";
      },

      openCustomers() {
        this.picking = true;
        this.customerQuery = "";
        this.findCustomers();
      },

      async findCustomers() {
        const r = await fetch(this.config.endpoints.customers + "?q=" +
                              encodeURIComponent(this.customerQuery.trim()));
        this.customerResults = r.ok ? (await r.json()).results : [];
      },

      async chooseCustomer(c) {
        this.customer = c;
        this.picking = false;
        await this.reprice();
      },

      async clearCustomer() {
        if (this.method === "credit") this.method = "";
        this.customer = null;
        await this.reprice();
      },

      async reprice() {
        // By id, in one request: looking them up by name missed products
        // with more than one variant, so the shown price was not the charged one.
        if (!this.lines.length) return;
        const ids = this.lines.map((l) => l.variant_id).join(",");
        const r = await fetch(this.config.endpoints.lookup + "?ids=" + encodeURIComponent(ids) +
                              this.customerParam());
        if (r.ok) {
          const found = (await r.json()).results;
          for (const line of this.lines) {
            const hit = found.find((x) => x.id === line.variant_id);
            if (hit && hit.price !== null) line.price = parseFloat(hit.price);
          }
        }
        this.saleId = null;
        this.save();
      },

      get methods() {
        const list = this.config.methods.slice();
        if (this.customer && this.config.may_credit) list.push({ value: "credit", label: "On account" });
        return list;
      },

      async search() {
        const q = this.query.trim();
        if (q.length < 2) { this.results = []; return; }
        const r = await fetch(this.config.endpoints.lookup + "?q=" + encodeURIComponent(q) +
                              this.customerParam());
        this.results = r.ok ? (await r.json()).results : [];
      },

      add(item, qty) {
        if (item.price === null || item.price === undefined) {
          this.status = item.name + " has no price yet. Ask a manager to set one.";
          return;
        }
        const existing = this.lines.find((l) => l.variant_id === item.id);
        const n = qty || parseFloat(item.qty || 1);
        if (existing) existing.qty = +(existing.qty + n).toFixed(3);
        else this.lines.push({ variant_id: item.id, name: item.name, price: parseFloat(item.price), qty: n, decimal: item.decimal });
        this.status = "";
        this.saleId = null;  // the basket changed: a new sale when paid
      },

      change(line, delta) {
        const step = line.decimal ? 0.5 : 1;
        line.qty = +(line.qty + delta * step).toFixed(3);
        if (line.qty <= 0) this.lines = this.lines.filter((l) => l !== line);
        this.saleId = null;
      },

      async addByCode(code) {
        const r = await fetch(this.config.endpoints.lookup + "?code=" + encodeURIComponent(code) +
                              this.customerParam());
        const data = r.ok ? await r.json() : { results: [] };
        if (data.results.length) {
          this.add(data.results[0]);
          this.scanHint = "Added " + data.results[0].name;
          if (navigator.vibrate) navigator.vibrate(60);
        } else {
          this.scanHint = data.error || "Not found";
        }
      },

      async startScan() {
        this.scanning = true;
        this.scanHint = "Point the camera at a barcode";
        if (!("BarcodeDetector" in window) || !navigator.mediaDevices) {
          this.scanHint = "This phone's browser cannot read barcodes. Type the number below.";
          return;
        }
        try {
          this.detector = new BarcodeDetector();
          this.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
          this.$refs.video.srcObject = this.stream;
          await this.$refs.video.play();
          let last = "";
          const tick = async () => {
            if (!this.scanning) return;
            try {
              const found = await this.detector.detect(this.$refs.video);
              if (found.length && found[0].rawValue !== last) {
                last = found[0].rawValue;
                await this.addByCode(last);
                setTimeout(() => { last = ""; }, 1500);  // the same item again, deliberately
              }
            } catch (e) { /* a frame it could not read */ }
            requestAnimationFrame(tick);
          };
          tick();
        } catch (e) {
          this.scanHint = "No camera access. Type the barcode below.";
        }
      },

      stopScan() {
        this.scanning = false;
        if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
        this.stream = null;
      },

      async typedCode() {
        const code = this.code.trim();
        if (!code) return;
        this.code = "";
        await this.addByCode(code);
      },

      async handoff() {
        if (this.busy) return;
        this.busy = true;
        this.status = "";
        try {
          const r = await fetch(this.config.endpoints.handoff, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
            body: JSON.stringify({
              lines: this.lines.map((l) => ({ variant_id: l.variant_id, qty: l.qty })),
              customer_id: this.customer ? this.customer.id : null,
            }),
          });
          const data = await r.json();
          if (!r.ok) { this.status = data.error || "Could not send it."; return; }
          this.done = { code: data.code, total: data.total };
          this.lines = [];
          this.customer = null;
        } catch (e) {
          this.status = "No connection. Try again.";
        } finally {
          this.busy = false;
        }
      },

      async pay() {
        if (this.busy) return;
        this.busy = true;
        this.error = "";
        // One id per basket: pressing again, or a lost reply, finds the
        // same sale instead of making a second.
        if (!this.saleId) { this.saleId = crypto.randomUUID(); this.save(); }
        try {
          const r = await fetch(this.config.endpoints.checkout, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
            body: JSON.stringify({
              client_uuid: this.saleId, method: this.method, reference: this.reference,
              customer_id: this.customer ? this.customer.id : null,
              device_id: (() => { try { return localStorage.getItem("phone-device-id-" + config.tenant_id); } catch (e) { return null; } })(),
              lines: this.lines.map((l) => ({ variant_id: l.variant_id, qty: l.qty })),
            }),
          });
          const data = await r.json();
          if (!r.ok) { this.error = data.error || "Not taken."; return; }
          this.done = { number: data.number, total: data.total,
                        onAccount: this.method === "credit" && this.customer ? this.customer.name : "" };
          this.paying = false;
          this.lines = [];
          this.saleId = null;
          this.customer = null;
          this.save();
        } catch (e) {
          this.error = "No connection. Nothing was charged here; try again.";
        } finally {
          this.busy = false;
        }
      },

      reset() {
        this.done = null;
        this.method = "";
        this.reference = "";
        this.error = "";
        this.lines = [];
        this.saleId = null;
        this.customer = null;
        this.save();
      },
    };
  };
})();
