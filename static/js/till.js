/*
 * The till.
 *
 * This is the one screen in the system that is not server-rendered, because a
 * shop must keep selling when the connection drops. The catalogue and the
 * queue of unsent sales live in IndexedDB; every sale gets a UUID generated
 * here, before it is sent, which is what makes a retried sync harmless.
 */
(function () {
  "use strict";

  const DB_VERSION = 1;

  let db = null;

  function openDb(name) {
    // One database per shop and person. A single shared one handed the next
    // cashier's till the previous one's unsent sales -- sent under the new
    // cashier's name and shift -- and another shop's catalogue.
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(name, DB_VERSION);
      request.onupgradeneeded = (event) => {
        const database = event.target.result;
        if (!database.objectStoreNames.contains("variants")) {
          const store = database.createObjectStore("variants", { keyPath: "id" });
          store.createIndex("name", "name", { unique: false });
        }
        if (!database.objectStoreNames.contains("barcodes")) {
          database.createObjectStore("barcodes", { keyPath: "code" });
        }
        if (!database.objectStoreNames.contains("queue")) {
          database.createObjectStore("queue", { keyPath: "client_uuid" });
        }
        if (!database.objectStoreNames.contains("meta")) {
          database.createObjectStore("meta", { keyPath: "key" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  function plain(value) {
    // Alpine hands out reactive proxies, which IndexedDB cannot store.
    return JSON.parse(JSON.stringify(value));
  }

  function request(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  function committed(transaction) {
    return new Promise((resolve, reject) => {
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  }

  async function adoptLegacyQueue(target) {
    // Before per-shop databases, every till used one called "till". Sales
    // still waiting there were taken and must not be lost -- but nobody can
    // tell whose they are, so they are held for a manager, never sent
    // automatically under whoever happens to be signed in now.
    if (!indexedDB.databases) return;
    const names = (await indexedDB.databases()).map((d) => d.name);
    if (!names.includes("till")) return;
    const legacy = await request(indexedDB.open("till", DB_VERSION));
    try {
      if (legacy.objectStoreNames.contains("queue")) {
        // A read error throws: the old database is then left alone.
        const waiting = await request(
          legacy.transaction("queue", "readonly").objectStore("queue").getAll()
        );
        if (waiting.length) {
          const write = target.transaction("queue", "readwrite");
          for (const sale of waiting) {
            sale.refused =
              "Recorded before an update, on a till whose cashier is not known. " +
              "A manager must check it before it is sent.";
            write.objectStore("queue").put(sale);
          }
          await committed(write);
          const copied = await request(
            target.transaction("queue", "readonly").objectStore("queue").getAll()
          );
          const have = new Set(copied.map((s) => s.client_uuid));
          if (!waiting.every((s) => have.has(s.client_uuid))) return;  // keep the old one
        }
      }
    } finally {
      legacy.close();
    }
    indexedDB.deleteDatabase("till");
  }

  function tx(store, mode) {
    return db.transaction(store, mode).objectStore(store);
  }

  function put(store, value) {
    return new Promise((resolve, reject) => {
      const request = tx(store, "readwrite").put(value);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  }

  function getAll(store) {
    return new Promise((resolve, reject) => {
      const request = tx(store, "readonly").getAll();
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error);
    });
  }

  function get(store, key) {
    return new Promise((resolve, reject) => {
      const request = tx(store, "readonly").get(key);
      request.onsuccess = () => resolve(request.result || null);
      request.onerror = () => reject(request.error);
    });
  }

  function remove(store, key) {
    return new Promise((resolve, reject) => {
      const request = tx(store, "readwrite").delete(key);
      request.onsuccess = () => resolve();
      request.onerror = () => reject(request.error);
    });
  }

  function deviceId(tenantId) {
    // One per browser and shop, random. "till-" + the register's name was
    // the same in every shop ("till-Till 1"), and device ids are unique
    // system-wide -- so a second shop on the same browser needs its own.
    const key = "till-device-id-" + tenantId;
    try {
      let id = localStorage.getItem(key);
      if (!id) {
        id = "till-" + crypto.randomUUID();
        localStorage.setItem(key, id);
      }
      return id;
    } catch (error) {
      return "till-unsaved";
    }
  }

  function csrf() {
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : "";
  }

  window.till = function (config) {
    return {
      config: config,
      lines: [],
      query: "",
      results: [],
      ready: false,
      online: navigator.onLine,
      queued: 0,
      catalogueCount: 0,
      status: "",
      showPayment: false,
      // In-page dialogs. Browser prompt() boxes block the page, cannot be
      // styled, and are easy to dismiss by accident mid-sale.
      dialog: null, // "open_item" | "pull_cart"
      openItemName: "",
      openItemPrice: "",
      cartCode: "",
      paying: false,
      blocked: false,
      refused: [],
      tendered: "",
      payMethod: "cash",
      reference: "",
      scanBuffer: "",
      scanTimer: null,
      // Until when the last message must stay on screen: sending the queue
      // wiped "Sale recorded" a second after the cashier saw it.
      holdUntil: 0,
      // Who is buying, if anyone: their price list prices the basket and
      // they can be sold to on account.
      customer: null,
      customers: [],
      priceLists: {},
      customerQuery: "",
      byId: {},
      // Narrow and upright: the basket is a panel at the bottom that opens,
      // so the tiles keep the screen. Wider: it sits beside them.
      wide: true,
      basketOpen: false,
      // Which category tab is showing. Empty means all of them.
      tileGroup: "",
      // A name and a phone for somebody who is not on the books. Neither is
      // required, and neither adds anybody to the customer list.
      buyerName: "",
      buyerPhone: "",
      // The product waiting on a quantity, and the number typed for it.
      qtyFor: null,
      qtyWanted: 1,
      // A newer version of the till is cached and waiting. Taken at the next
      // safe moment, which is never in the middle of somebody's shopping.
      updateWaiting: false,

      async init() {
        db = await openDb("till-" + this.config.tenant_id + "-" + this.config.user_id);
        try {
          await adoptLegacyQueue(db);
        } catch (error) {
          /* the old database stays put and is tried again next time */
        }
        await this.loadCatalogue();
        await this.loadCustomers();
        await this.countQueue();
        this.ready = true;
        this.register();

        window.addEventListener("online", () => {
          this.online = true;
          this.flush();
        });
        window.addEventListener("offline", () => {
          this.online = false;
        });

        // A hardware scanner is a keyboard that types fast and ends with
        // Enter. Catching it at the document means the cashier never has to
        // click into the search box first.
        document.addEventListener("keydown", (event) => this.onKey(event));

        const wideEnough = window.matchMedia("(min-width: 768px)");
        this.wide = wideEnough.matches;
        wideEnough.addEventListener("change", (event) => {
          this.wide = event.matches;
          if (event.matches) this.basketOpen = false;
        });

        if (this.online) this.flush();
        setInterval(() => this.flush(), 30000);
      },

      // ---- catalogue --------------------------------------------------

      async loadCatalogue() {
        const cached = await getAll("variants");
        this.catalogueCount = cached.length;
        if (this.online) await this.refreshCatalogue();
        await this.indexTiles();
      },

      async indexTiles() {
        // Every product's prices by id, so a tile shows the customer's price.
        const byId = {};
        for (const v of await getAll("variants")) byId[v.id] = v;
        this.byId = byId;
      },

      tilePrice(id, usual) {
        const variant = this.byId && this.byId[id];
        const price = variant ? this.priceFor(variant) : usual;
        return price === null || price === undefined || price === "" ? "—" : this.money(price);
      },

      async refreshCatalogue() {
        try {
          const meta = await get("meta", "catalog_since");
          const url =
            this.config.endpoints.catalog + (meta ? "?since=" + encodeURIComponent(meta.value) : "");
          const response = await fetch(url, { headers: { Accept: "application/json" } });
          if (!response.ok) return;
          const data = await response.json();

          const removed = new Set(data.removed || []);
          for (const id of removed) {
            // Switched off or deleted since the last sync: stop selling it,
            // including by scanning its barcode.
            await remove("variants", id);
          }
          if (removed.size) {
            for (const code of await getAll("barcodes")) {
              if (removed.has(code.variant_id)) await remove("barcodes", code.code);
            }
          }
          const updated = new Set(data.variants.map((v) => v.id));
          if (updated.size) {
            // Barcodes taken off a product must stop scanning as it.
            for (const code of await getAll("barcodes")) {
              if (updated.has(code.variant_id)) await remove("barcodes", code.code);
            }
          }
          for (const variant of data.variants) {
            await put("variants", variant);
            for (const barcode of variant.barcodes) {
              await put("barcodes", {
                code: barcode.code,
                variant_id: variant.id,
                qty: parseFloat(barcode.qty),
              });
            }
          }
          await put("meta", { key: "catalog_since", value: data.server_time });
          this.catalogueCount = (await getAll("variants")).length;
          this.say("");
        } catch (error) {
          this.say("Working offline");
        }
      },

      // ---- customers --------------------------------------------------

      async loadCustomers() {
        const cached = await get("meta", "customers");
        if (cached) {
          this.customers = cached.value.customers || [];
          this.priceLists = cached.value.price_lists || {};
        }
        if (!this.online || !this.config.endpoints.customers) return;
        try {
          const response = await fetch(this.config.endpoints.customers,
                                       { headers: { Accept: "application/json" } });
          if (!response.ok) return;
          const data = await response.json();
          this.customers = data.customers || [];
          this.priceLists = data.price_lists || {};
          await put("meta", { key: "customers", value: data });
        } catch (error) { /* offline: the cached list stays */ }
      },

      get customerMatches() {
        const term = this.customerQuery.trim().toLowerCase();
        const all = this.customers;
        if (!term) return all.slice(0, 20);
        return all.filter((c) => c.name.toLowerCase().includes(term) ||
                                 (c.phone || "").includes(term)).slice(0, 20);
      },

      openCustomers() {
        this.customerQuery = "";
        this.dialog = "customer";
        this.loadCustomers();
      },

      async chooseCustomer(customer) {
        this.customer = customer;
        this.dialog = null;
        await this.reprice();
      },

      async clearCustomer() {
        if (this.payMethod === "credit") this.payMethod = "cash";
        this.customer = null;
        await this.reprice();
      },

      get customerListName() {
        const list = this.customer && this.customer.price_list;
        return list && this.priceLists[String(list)] ? this.priceLists[String(list)] : "";
      },

      priceFor(variant) {
        // The customer's list when it has this product; otherwise the usual price.
        const list = this.customer && this.customer.price_list;
        if (list && variant.prices && variant.prices[String(list)] !== undefined) {
          return variant.prices[String(list)];
        }
        return variant.price;
      },

      async reprice() {
        // Lines from the catalogue follow the customer. Open items and
        // baskets built on a phone keep the price they were given.
        for (const line of this.lines) {
          if (!line.variant_id || line.cart_line_id || line.added_via === "manual") continue;
          const variant = await get("variants", line.variant_id);
          const price = variant ? this.priceFor(variant) : null;
          if (price !== null && price !== undefined) line.unit_price = parseFloat(price);
        }
      },

      get creditLeft() {
        return this.customer ? parseFloat(this.customer.credit_left || 0) : 0;
      },

      get payMethods() {
        const methods = ["cash", "mpesa", "tigopesa", "airtelmoney"];
        if (this.customer && this.config.permissions.credit) methods.push("credit");
        return methods;
      },

      methodLabel(method) {
        return { airtelmoney: "Airtel", tigopesa: "Tigo", mpesa: "M-Pesa", credit: "On account",
                 cash: "Cash" }[method] || method;
      },

      // ---- input ------------------------------------------------------

      onKey(event) {
        if (this.showPayment || this.dialog) {
          if (event.key === "Escape") {
            this.showPayment = false;
            this.dialog = null;
          }
          return;
        }
        if (event.target.tagName === "INPUT" && event.target.type !== "search") return;

        // Scanners are configured to end a code with Enter or with Tab, and
        // the shop does not always know which its reader sends. Both finish a
        // scan; the buffer has already cleared itself if a person was typing.
        if (event.key === "Enter" || event.key === "Tab") {
          const code = this.scanBuffer;
          this.scanBuffer = "";
          if (code.length >= 2) {
            // Short internal codes ("A7B") are real in small shops, but two
            // characters a person typed are not: an unknown short code says
            // nothing rather than crying wolf.
            this.scan(code, { quiet: code.length < 4 });
            event.preventDefault();
          }
          return;
        }
        if (event.key.length === 1) {
          this.scanBuffer += event.key;
          clearTimeout(this.scanTimer);
          // A human typing is slower than any scanner; flush the buffer so
          // typed characters never look like a scan.
          this.scanTimer = setTimeout(() => (this.scanBuffer = ""), 120);
        }
      },

      async scan(code, { quiet = false } = {}) {
        const hit = await get("barcodes", code.trim());
        if (!hit) {
          if (!quiet) this.status = "Unknown barcode " + code;
          return;
        }
        const variant = await get("variants", hit.variant_id);
        if (!variant) return;
        this.addLine(variant, hit.qty || 1, "scan");
        // A scan made with the cursor in the search box used to leave the
        // barcode sitting there, with stale results under it.
        this.query = "";
        this.results = [];
      },

      async search() {
        const term = this.query.trim().toLowerCase();
        if (term.length < 2) {
          this.results = [];
          return;
        }
        const all = await getAll("variants");
        this.results = all
          .filter(
            (v) =>
              v.name.toLowerCase().includes(term) ||
              (v.sku || "").toLowerCase().includes(term)
          )
          .slice(0, 12);
      },

      pick(variant) {
        this.addLine(variant, 1, "search");
        this.query = "";
        this.results = [];
      },

      async addTile(id, name, price, taxRate, image, qty) {
        // The stored product carries every list's price; the tile only the usual one.
        const stored = await get("variants", id);
        this.addLine(
          stored || { id: id, name: name, price: price, tax_rate: taxRate,
                      decimal: false, image: image || null },
          qty || 1,
          "tile"
        );
      },

      get basketShown() {
        return this.wide || this.basketOpen;
      },

      addLine(variant, qty, via) {
        // No price on file is not "free": it used to ring up at 0.
        const listed = this.priceFor(variant);
        if (listed === null || listed === undefined || listed === "") {
          this.status = variant.name + " has no price yet. Ask a manager to set one.";
          return;
        }
        const price = parseFloat(listed);
        const existing = this.lines.find(
          (line) => line.variant_id === variant.id && line.unit_price === price
        );
        if (existing) {
          existing.qty += qty;
          return;
        }
        this.lines.push({
          variant_id: variant.id,
          description: variant.name,
          qty: qty,
          unit_price: price,
          discount: 0,
          tax_rate: parseFloat(variant.tax_rate || 0),
          added_via: via,
          decimal: variant.decimal,
          // For the thumbnail beside the line in the order summary. Kept on
          // the line itself so it survives the basket being restored.
          image: variant.image || null,
        });
      },

      askQty(id, name, price, taxRate, image) {
        // Twelve sodas at a time is a normal order, and tapping a tile twelve
        // times is how a queue builds up.
        this.qtyFor = { id, name, price, taxRate, image };
        this.qtyWanted = 1;
        this.dialog = "qty";
      },

      addWanted() {
        const want = parseFloat(this.qtyWanted);
        if (!this.qtyFor || !(want > 0)) return;
        const item = this.qtyFor;
        this.dialog = null;
        this.addTile(item.id, item.name, item.price, item.taxRate, item.image, want);
        this.qtyFor = null;
      },

      openItem() {
        if (!this.config.permissions.open_item) return;
        this.openItemName = "";
        this.openItemPrice = "";
        this.dialog = "open_item";
      },

      addOpenItem() {
        const description = this.openItemName.trim();
        const price = parseFloat(this.openItemPrice || 0);
        if (!description || !(price > 0)) return;
        const limit = this.config.permissions.open_item_limit;
        if (limit && price > limit) {
          // The server refuses it anyway; say so now, not after the sale.
          this.status = "Open items are limited to " + this.money(limit) + " for you";
          return;
        }
        this.dialog = null;
        this.lines.push({
          variant_id: null,
          description: description,
          qty: 1,
          unit_price: price,
          discount: 0,
          tax_rate: 0,
          added_via: "manual",
        });
      },

      changeQty(line, delta) {
        line.qty = Math.max(line.decimal ? 0.001 : 1, line.qty + delta);
      },

      removeLine(index) {
        this.lines.splice(index, 1);
      },

      takeUpdateIfSafe() {
        // Reloading throws away the basket, so it only happens with nothing
        // in it and no payment on the screen.
        if (!this.updateWaiting || this.paying || this.showPayment) return;
        if (this.lines.length) return;
        location.reload();
      },

      clear() {
        this.lines = [];
        this.showPayment = false;
        this.tendered = "";
        // Otherwise the next M-Pesa sale silently reused this reference.
        this.reference = "";
        this.payMethod = "cash";
        // The next buyer is somebody else until the cashier says otherwise.
        this.customer = null;
        this.buyerName = "";
        this.buyerPhone = "";
        // The moment the screen is empty is the moment it is safe to
        // pick up a newer version of the till.
        this.takeUpdateIfSafe();
      },

      flash(message, seconds = 4) {
        this.status = message;
        this.holdUntil = Date.now() + seconds * 1000;
      },

      say(message) {
        // Background chatter: never overwrites something the cashier is
        // still meant to be reading.
        if (Date.now() < this.holdUntil) return;
        this.status = message;
      },

      get overCredit() {
        // What the server would refuse: stop before the goods leave.
        if (this.payMethod !== "credit") return "";
        const limit = this.config.permissions.credit_limit;
        if (limit && this.subtotal > limit) {
          return "On account is limited to " + this.money(limit) + " for you.";
        }
        return "";
      },

      get shortPaid() {
        // Cash handed over must cover the sale. Left empty means exact money.
        if (this.payMethod !== "cash" || this.tendered === "") return false;
        return parseFloat(this.tendered || 0) + 0.005 < this.subtotal;
      },

      // ---- totals -----------------------------------------------------

      get totalItems() {
        // What the customer is carrying, not how many lines it took.
        return this.lines.reduce((sum, line) => sum + (parseFloat(line.qty) || 0), 0);
      },

      get subtotal() {
        return this.lines.reduce(
          (sum, line) => sum + line.qty * line.unit_price - line.discount,
          0
        );
      },

      get tax() {
        return this.lines.reduce((sum, line) => {
          const net = line.qty * line.unit_price - line.discount;
          const rate = line.tax_rate;
          return sum + (rate ? (net * rate) / (100 + rate) : 0);
        }, 0);
      },

      get change() {
        const given = parseFloat(this.tendered || 0);
        return Math.max(0, given - this.subtotal);
      },

      money(value) {
        return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(
          Math.round(value || 0)
        );
      },

      // ---- completing -------------------------------------------------

      async pay() {
        // A second tap while the first is still saving used to record the
        // same basket twice, under two ids: charged twice, stock out twice.
        if (this.paying || !this.lines.length || this.shortPaid || this.blocked) return;
        if (this.overCredit) { this.status = this.overCredit; return; }
        this.paying = true;
        const lines = this.lines;
        const total = this.subtotal;
        const change = this.change;
        const method = this.payMethod;
        const reference = this.reference;

        const sale = {
          client_uuid: crypto.randomUUID(),
          sold_at: new Date().toISOString(),
          shift_id: this.config.shift_id,
          customer_id: this.customer ? this.customer.id : null,
          // Only meaningful without a customer on the books; a customer's own
          // name is already on their account.
          buyer_name: this.customer ? "" : this.buyerName.trim(),
          buyer_phone: this.customer ? "" : this.buyerPhone.trim(),
          tenant_id: this.config.tenant_id,
          user_id: this.config.user_id,
          lines: lines.map((line) => ({
            variant_id: line.variant_id,
            description: line.description,
            qty: line.qty,
            unit_price: line.unit_price,
            discount: line.discount,
            added_via: line.added_via,
            cart_line_id: line.cart_line_id || null,
          })),
          payments: [
            { method: method, amount: total, reference: reference,
              change_given: method === "cash" ? change : 0 },
          ],
        };

        // Queue first, send second. If the browser dies between the two, the
        // sale is still on the device rather than lost.
        try {
          await put("queue", plain(sale));
        } catch (error) {
          // Not saved: keep the basket on screen so nothing is lost.
          this.paying = false;
          this.status = "Could not save the sale on this device. Try again.";
          return;
        }
        // Only now, with the sale safely stored, clear the screen.
        const onAccount = method === "credit" && this.customer;
        if (onAccount) {
          // Keep the offline figure honest until the next refresh.
          this.customer.credit_left = String(Math.max(0, this.creditLeft - total));
        }
        this.clear();
        this.paying = false;
        await this.countQueue();
        this.flash(onAccount ? "Sale recorded on account" : "Sale recorded");

        if (this.online) this.flush();
      },

      async register() {
        // Introduce this till once per shop, so it is listed -- and can be
        // switched off if it is lost. Quietly retried next time if offline.
        const key = "till-registered-" + this.config.tenant_id;
        try {
          if (localStorage.getItem(key)) return;
        } catch (e) { /* no storage: register every time, harmlessly */ }
        try {
          const r = await fetch(this.config.endpoints.devices, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
            body: JSON.stringify({ device_id: deviceId(this.config.tenant_id), kind: "till",
                                   label: this.config.register }),
          });
          if (r.ok) localStorage.setItem(key, "1");
          if (r.status === 409 && (await r.json().catch(() => ({}))).error === "device_retired") {
            this.blocked = true;
            this.status = "This till has been switched off by the shop. Ask the owner.";
          }
        } catch (e) { /* offline */ }
      },

      async countQueue() {
        const all = await getAll("queue");
        this.refused = all.filter((sale) => sale.refused);
        this.queued = all.length - this.refused.length;
      },

      async retryRefused(sale) {
        const copy = plain(sale);
        delete copy.refused;
        await put("queue", copy);
        await this.countQueue();
        this.flush();
      },

      async discardRefused(sale) {
        await remove("queue", sale.client_uuid);
        await this.countQueue();
      },

      async flush() {
        if (this.blocked) return;  // somebody else is signed in; nothing is sent
        // Refused sales wait for a person; resending them every 30 seconds
        // only filled the audit log.
        const pending = (await getAll("queue")).filter((sale) => !sale.refused);
        if (!pending.length) {
          await this.countQueue();
          return;
        }

        try {
          const response = await fetch(this.config.endpoints.sales, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": csrf(),
            },
            body: JSON.stringify({
              // Whose queue this is. If somebody else has signed in on this
              // browser since, the server refuses rather than filing these
              // sales under their name and into their drawer.
              queue_owner: { tenant_id: this.config.tenant_id, user_id: this.config.user_id },
              // Each sale carries the shift it was made in; the batch value
              // is only for sales queued before that existed.
              sales: pending,
              shift_id: this.config.shift_id,
              device_id: deviceId(this.config.tenant_id),
              queued: pending.length,
            }),
          });
          if (response.status === 409) {
            const why = (await response.json().catch(() => ({}))).error;
            this.blocked = true;
            this.status = why === "device_unknown"
              ? "This till has not introduced itself to the shop yet. Reload the page."
              : why === "device_retired"
              ? "This till has been switched off by the shop. " + pending.length +
                " sale(s) are kept here; ask the owner."
              : "Someone else is signed in. Sign back in as " + this.config.cashier +
                " to send " + pending.length + " waiting sale(s).";
            return;
          }
          if (!response.ok) return;

          const data = await response.json();
          for (const entry of data.accepted) {
            await remove("queue", entry.client_uuid);
          }
          // A refused sale stays on the device, visible, until somebody
          // retries or removes it -- it is never silently dropped.
          for (const entry of data.rejected) {
            const sale = pending.find((s) => s.client_uuid === entry.client_uuid);
            if (sale) {
              sale.refused = entry.error;
              await put("queue", sale);
            }
          }
          for (const entry of data.retry || []) {
            // A server error that keeps happening is not "waiting to send"
            // for ever: after enough tries it goes in front of a manager.
            const sale = pending.find((s) => s.client_uuid === entry.client_uuid);
            if (sale) {
              sale.retries = (sale.retries || 0) + 1;
              if (sale.retries >= 20) sale.refused = "The server could not store this sale.";
              await put("queue", sale);
            }
          }
          await this.countQueue();
          this.online = true;
          this.say(this.refused.length
            ? this.refused.length + " refused, needs a manager"
            : this.queued ? this.queued + " waiting to send" : "");
        } catch (error) {
          this.online = false;
          this.say("Offline, " + pending.length + " held");
        }
      },

      pullCart() {
        this.cartCode = "";
        this.dialog = "pull_cart";
      },

      async collectCart() {
        const code = this.cartCode.trim();
        if (!code) return;
        this.dialog = null;
        try {
          const response = await fetch(
            this.config.endpoints.cart_pull + code.toUpperCase() + "/",
            { method: "POST", headers: { "X-CSRFToken": csrf() } }
          );
          if (!response.ok) {
            this.status = "No basket with that code";
            return;
          }
          const data = await response.json();
          for (const line of data.lines) {
            this.lines.push({
              variant_id: line.variant_id,
              cart_line_id: line.cart_line_id,
              description: line.description,
              qty: parseFloat(line.qty),
              unit_price: parseFloat(line.unit_price),
              discount: parseFloat(line.discount),
              tax_rate: parseFloat(line.tax_rate),
              added_via: line.added_via,
              // Without it "−" on a half-kilo line jumped it up to one.
              decimal: !!line.decimal,
            });
          }
          if (data.customer_id) {
            // Built for a customer on the phone: they arrive with the basket,
            // already priced from their list.
            this.customer = this.customers.find((c) => c.id === data.customer_id) ||
              { id: data.customer_id, name: data.customer, price_list: null, credit_left: "0" };
          }
          this.status = "Basket from " + data.built_by + (data.customer ? " for " + data.customer : "");
        } catch (error) {
          this.status = "Cannot reach the server for baskets";
        }
      },
    };
  };
})();
