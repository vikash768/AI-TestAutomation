const { app, Menu, shell } = require('electron');
const zoomIn = { label: 'Zoom In', accelerator: 'CmdOrCtrl+Plus', click() { win.webContents.zoomIn(); } };
const template = [{
  label: app.name,
  submenu: [
    { label: 'About ' + app.name, click() { shell.openExternal('https://example.com'); } },
    ...(updates ? [zoomIn] : []),
    { type: 'separator' },
    { role: 'quit' }
  ]
}, {
  label: 'Edit',
  submenu: [{ role: 'undo' }, { role: 'selectAll' }]
}];
Menu.setApplicationMenu(Menu.buildFromTemplate(template));
