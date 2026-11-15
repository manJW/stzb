'use strict';
rpc.exports = {
  run(name, bytes, hash) {
    const own = STLabManaged.load(name, bytes, hash);
    own.call('Reset', [], 'void'); own.call('Bump', [3], 'void');
    const initial = own.call('Count', [], 'int');
    own.call('Reset', [], 'void');
    const result = own.call('Run', [], 'string');
    const final = own.call('Count', [], 'int');
    own.call('Reset', [], 'void');
    return {passed: initial === 3 && final === 0 && result === 'SELF_TEST_OK', initial, final, result,
      managedLoaderUsed:true, sharedLoader:true, perCallThreadAttach:true, gameRequestsSent:0, gameCallbacksInstalled:0};
  }
};
