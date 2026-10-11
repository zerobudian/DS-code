var Dialogs = (function () {
  var active = null;
  function close() { if (active) active(null); }
  function show(message, fields) {
    close();
    return new Promise(function (resolve) {
      var overlay = document.createElement('div'); overlay.className = 'dialog-overlay';
      var panel = document.createElement('form'); panel.className = 'dialog-panel';
      var text = document.createElement('pre'); text.textContent = message; panel.appendChild(text);
      var inputs = (fields || []).map(function (field) {
        var label = document.createElement('label'); label.textContent = field.label;
        var input = document.createElement('textarea'); input.value = field.value || '';
        label.appendChild(input); panel.appendChild(label); return input;
      });
      var buttons = document.createElement('div'); buttons.className = 'dialog-buttons';
      var cancel = document.createElement('button'); cancel.type = 'button';
      cancel.textContent = I18N.current === 'en' ? 'Cancel' : '取消';
      var accept = document.createElement('button'); accept.type = 'submit';
      accept.textContent = I18N.current === 'en' ? 'Confirm' : '确认';
      buttons.appendChild(cancel); buttons.appendChild(accept); panel.appendChild(buttons);
      overlay.appendChild(panel); document.body.appendChild(overlay);
      function finish(value) { overlay.remove(); active = null; resolve(value); }
      active = finish; cancel.onclick = function () { finish(null); };
      panel.onsubmit = function (event) { event.preventDefault(); finish(inputs.map(function (input) { return input.value; })); };
      (inputs[0] || accept).focus();
    });
  }
  return { show: show, close: close };
})();
