'use strict';
let identityReader;
rpc.exports = {
  initialize(name, bytes, digest) { identityReader = STLabManaged.load(name, bytes, digest); },
  identify() { if (!identityReader) throw new Error('Identity component not initialized'); return JSON.parse(identityReader.call('Identify', [], 'string')); },
  cancel() {}
};
