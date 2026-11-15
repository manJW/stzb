'use strict';
let selectionReader;
rpc.exports = {
  initialize(name, bytes, digest, users, server) {
    selectionReader = STLabManaged.load(name, bytes, digest);
    selectionReader.call('Start', [Number(users[0]), Number(users[1]), server], 'void');
  },
  read() {
    if (!selectionReader) throw new Error('Selection reader not initialized');
    return JSON.parse(selectionReader.call('Read', [], 'string'));
  },
  cancel() { if (selectionReader) selectionReader.call('Stop', [], 'void'); }
};
