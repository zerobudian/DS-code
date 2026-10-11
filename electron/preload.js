const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('dscodeApi', {
  bootstrap: () => ipcRenderer.invoke('get-sidecar-config'),
});
