'use strict';
let reader = null;
let identityReader = null;
const state = () => JSON.parse(reader.call('Status', [], 'string'));
rpc.exports = {
  initializeIdentity(name, bytes, digest) { identityReader=STLabManaged.load(name,bytes,digest); },
  identify() { if(!identityReader)throw new Error('Identity verification unavailable');return JSON.parse(identityReader.call('Identify',[],'string')); },
  initialize(name, bytes, digest) {
    if (reader) throw new Error('Reader already initialized');
    reader = STLabManaged.load(name, bytes, digest);
    reader.call('Cancel', [], 'void');
    return state();
  },
  queue(users, server, kind, value, ids) {
    if (!reader || ![0, 1, 2, 3].includes(kind)) throw new Error('Unsupported report/member read');
    if (kind === 0) {
      if (!Array.isArray(ids) || ids.length < 1 || ids.length > 15) throw new Error('Invalid profile batch');
      reader.call('ClearProfiles', [], 'void');
      for (const id of ids) reader.call('AddProfile', [id], 'void');
    }
    reader.call('Queue', [kind, value, Number(users[0]), Number(users[1]), server], 'void');
    return state();
  },
  status() { return state(); },
  cancel() { if (reader) reader.call('Cancel', [], 'void'); }
};
