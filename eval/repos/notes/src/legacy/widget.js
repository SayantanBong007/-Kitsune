// Legacy embeddable widget (pre-TypeScript)
(function () {
  function renderBadge(count) {
    return "<span class='badge'>" + count + "</span>";
  }

  function mountWidget(el, count) {
    el.innerHTML = renderBadge(count);
  }

  window.NotesWidget = { mountWidget: mountWidget };
})();
