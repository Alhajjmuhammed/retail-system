/*
 * Adding a line to a quotation or an invoice.
 *
 * Picking a product fills in what it would be charged at, the VAT it
 * carries and whether it can be sold in halves. Everything stays editable:
 * a quoted price is a negotiation, and typing over the suggestion is the
 * normal case, not an error.
 *
 * The running line total is shown while it is typed, because the argument a
 * shop has with a customer is almost never about the unit price -- it is
 * about what the line came to.
 */
window.lineEntry = function () {
  return {
    qty: "1",
    price: "",
    off: "0",
    step: "1",
    photo: "",
    code: "",

    picked: function (select) {
      var chosen = select.options[select.selectedIndex];
      if (!chosen || !chosen.value) {
        this.step = "1";
        this.photo = "";
        this.code = "";
        return;
      }
      // What the line should say, so nobody retypes the product name. It
      // stays editable: "Mkate Mkubwa, sliced" is a better line than the
      // catalogue name, and the customer reads this one.
      var description = this.$refs.desc;
      if (description && !description.value) description.value = chosen.dataset.desc || "";
      this.photo = chosen.dataset.img || "";
      this.code = chosen.dataset.code || "";

      var suggested = chosen.dataset.price;
      // A product with no price on the list leaves the box alone rather
      // than writing a zero somebody has to notice and delete.
      if (suggested) this.price = suggested;

      // Sugar is sold by the half kilo; tins are not.
      this.step = chosen.dataset.decimal === "1" ? "0.001" : "1";
      if (this.step === "1" && this.qty && String(this.qty).indexOf(".") !== -1) {
        this.qty = String(Math.max(1, Math.round(Number(this.qty))));
      }

      var tax = chosen.dataset.tax;
      if (tax && this.$refs.tax) this.$refs.tax.value = tax;
    },

    lineTotal: function () {
      var qty = Number(this.qty) || 0;
      var price = Number(this.price) || 0;
      var off = Number(this.off) || 0;
      var gross = qty * price;
      var total = gross - (gross * off) / 100;
      if (!total) return "—";
      return Math.round(total).toLocaleString();
    },
  };
};
