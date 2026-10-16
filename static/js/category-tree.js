/*
 * The categories page: a tree that folds and searches.
 *
 * A row is shown when nothing is being searched and every shelf above it is
 * open, or when the search appears in its own name, a parent's or anything
 * beneath it -- so a match keeps the shelves around it on screen. What is
 * open is remembered on this device, so coming back to the page after adding
 * a shelf finds it the way it was left.
 */
window.categoryTree = function (startOpen) {
  var KEY = "categories.open";
  return {
    q: "",
    inside: "",
    open: {},

    init: function () {
      var saved = null;
      try { saved = JSON.parse(window.localStorage.getItem(KEY) || "null"); } catch (e) { saved = null; }
      if (saved && typeof saved === "object") {
        this.open = saved;
      } else if (startOpen) {
        this.expandAll();
      }
    },

    remember: function () {
      try { window.localStorage.setItem(KEY, JSON.stringify(this.open)); } catch (e) { /* private window */ }
    },

    isOpen: function (pk) { return !!this.open[pk]; },

    toggle: function (pk) {
      this.open[pk] = !this.open[pk];
      this.remember();
    },

    expandAll: function () {
      var rows = document.querySelectorAll(".cat-row[data-pk]");
      for (var i = 0; i < rows.length; i++) this.open[rows[i].dataset.pk] = true;
      this.remember();
    },

    collapseAll: function () {
      this.open = {};
      this.remember();
    },

    term: function () { return this.q.trim().toLowerCase(); },

    visible: function (search, ancestors) {
      var term = this.term();
      if (term) return search.indexOf(term) !== -1;
      for (var i = 0; i < ancestors.length; i++) {
        if (!this.open[ancestors[i]]) return false;
      }
      return true;
    },

    anyMatch: function () {
      var term = this.term();
      var rows = document.querySelectorAll(".cat-row[data-search]");
      for (var i = 0; i < rows.length; i++) {
        if ((rows[i].dataset.search || "").indexOf(term) !== -1) return true;
      }
      return false;
    },

    addInside: function (pk, label) {
      this.inside = String(pk || "");
      var self = this;
      this.$nextTick(function () {
        var form = document.getElementById("new-category");
        if (form) form.scrollIntoView({ behavior: "smooth", block: "nearest" });
        if (self.$refs.catName) self.$refs.catName.focus();
      });
    },
  };
};
