'use strict';
const STLabManaged = {
  load(assemblyName, bytes, expectedDigest) {
    const match = /^STLab\.(InteropSelfTest|BattleReportReader|IdentityReader|MapSelectionReader)\.[a-f0-9]{12}\.dll$/.exec(assemblyName);
    if (!match || !Array.isArray(bytes) || bytes.length < 1024 || bytes.length > 65536) throw new Error('Unexpected bounded component');
    const mono = Process.findModuleByName('libmonosgen-2.0.so') || Process.enumerateModules().find(m => /^mono.*\.dll$/i.test(m.name) && m.findExportByName('mono_get_root_domain'));
    if (!mono) throw new Error('Supported Mono runtime not loaded');
    const fn = (name, ret, args) => new NativeFunction(mono.getExportByName(name), ret, args);
    const domain = fn('mono_get_root_domain', 'pointer', [])();
    const attach = fn('mono_thread_attach', 'pointer', ['pointer']);
    const detach = fn('mono_thread_detach', 'void', ['pointer']);
    const corlib = fn('mono_get_corlib', 'pointer', []);
    const klass = fn('mono_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer']);
    const methods = fn('mono_class_get_methods', 'pointer', ['pointer', 'pointer']);
    const methodName = fn('mono_method_get_name', 'pointer', ['pointer']);
    const signature = fn('mono_method_signature', 'pointer', ['pointer']);
    const paramCount = fn('mono_signature_get_param_count', 'uint32', ['pointer']);
    const parameter = fn('mono_signature_get_params', 'pointer', ['pointer', 'pointer']);
    const typeName = fn('mono_type_get_name', 'pointer', ['pointer']);
    const method = fn('mono_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int']);
    const invoke = fn('mono_runtime_invoke', 'pointer', ['pointer', 'pointer', 'pointer', 'pointer']);
    const arrayNew = fn('mono_array_new', 'pointer', ['pointer', 'pointer', 'ulong']);
    const byteClass = fn('mono_get_byte_class', 'pointer', []);
    const arrayAddress = fn('mono_array_addr_with_size', 'pointer', ['pointer', 'int', 'ulong']);
    const hold = fn('mono_gchandle_new', 'uint32', ['pointer', 'int']);
    const target = fn('mono_gchandle_get_target', 'pointer', ['uint32']);
    const release = fn('mono_gchandle_free', 'void', ['uint32']);
    const each = fn('mono_assembly_foreach', 'void', ['pointer', 'pointer']);
    const assemblyIdentity = fn('mono_assembly_get_name', 'pointer', ['pointer']);
    const identityName = fn('mono_assembly_name_get_name', 'pointer', ['pointer']);
    const imageOf = fn('mono_assembly_get_image', 'pointer', ['pointer']);
    const text = fn('mono_string_to_utf8', 'pointer', ['pointer']);
    const free = fn('mono_free', 'void', ['pointer']);
    const unbox = fn('mono_object_unbox', 'pointer', ['pointer']);
    const objectClass = fn('mono_object_get_class', 'pointer', ['pointer']);
    const className = fn('mono_class_get_name', 'pointer', ['pointer']);
    const classNamespace = fn('mono_class_get_namespace', 'pointer', ['pointer']);
    const withThread = action => {
      const thread = attach(domain);
      try { return action(); } finally { detach(thread); }
    };
    const callRaw = (entry, parameters) => {
      const exception = Memory.alloc(Process.pointerSize); exception.writePointer(ptr(0));
      const value = invoke(entry, ptr(0), parameters, exception);
      if (!exception.readPointer().isNull()) {
        const type = objectClass(exception.readPointer());
        throw new Error('Component invocation failed: ' + classNamespace(type).readUtf8String() + '.' + className(type).readUtf8String());
      }
      return value;
    };
    let ownImage = ptr(0), reused = false;
    const find = () => {
      const visit = new NativeCallback(assembly => {
        if (identityName(assemblyIdentity(assembly)).readUtf8String() === assemblyName.slice(0, -4)) ownImage = imageOf(assembly);
      }, 'void', ['pointer', 'pointer']);
      each(visit, ptr(0));
    };
    const own = withThread(() => {
      const payload = Memory.alloc(bytes.length); payload.writeByteArray(bytes);
      if (Checksum.compute('sha256', payload.readByteArray(bytes.length)) !== expectedDigest) throw new Error('Component hash mismatch');
      find(); reused = !ownImage.isNull();
      if (ownImage.isNull()) {
        const assemblyType = klass(corlib(), Memory.allocUtf8String('System.Reflection'), Memory.allocUtf8String('Assembly'));
        const iter = Memory.alloc(Process.pointerSize); iter.writePointer(ptr(0));
        let load = ptr(0), candidate;
        while (!(candidate = methods(assemblyType, iter)).isNull()) {
          if (methodName(candidate).readUtf8String() !== 'Load') continue;
          const sig = signature(candidate);
          if (paramCount(sig) !== 1) continue;
          const pi = Memory.alloc(Process.pointerSize); pi.writePointer(ptr(0));
          const name = typeName(parameter(sig, pi));
          try { if (name.readUtf8String() === 'System.Byte[]') { load = candidate; break; } } finally { free(name); }
        }
        if (load.isNull()) throw new Error('Managed byte-array loader unavailable');
        const handles = [];
        try {
          handles.push(hold(arrayNew(domain, byteClass(), bytes.length), 1));
          arrayAddress(target(handles[0]), 1, 0).writeByteArray(bytes);
          const args = Memory.alloc(Process.pointerSize); args.writePointer(target(handles[0]));
          const loaded = callRaw(load, args);
          if (loaded.isNull()) throw new Error('Managed loader returned no assembly');
          handles.push(hold(loaded, 1)); find();
        } finally { for (const handle of handles.reverse()) release(handle); }
      }
      if (ownImage.isNull()) throw new Error('Managed component image not registered');
      const type = klass(ownImage, Memory.allocUtf8String('STLab.' + match[1]), Memory.allocUtf8String('Entry'));
      if (type.isNull()) throw new Error('Bounded component entry unavailable');
      return type;
    });
    const allowed = match[1] === 'InteropSelfTest' ? {Reset:0,Bump:1,Count:0,Run:0} : match[1] === 'IdentityReader' ? {Identify:0}
      : match[1] === 'MapSelectionReader' ? {Start:3,Read:0,Stop:0} : {Queue:5,Status:0,Cancel:0,ClearProfiles:0,AddProfile:1};
    return {
      reused,
      call(name, values, mode) {
        if (!(name in allowed) || values.length !== allowed[name] || values.some(x => !Number.isInteger(x) || x < -2147483648 || x > 2147483647))
          throw new Error('Unexpected component operation');
        return withThread(() => {
          const entry = method(own, Memory.allocUtf8String(name), values.length);
          if (entry.isNull()) throw new Error('Component entry unavailable');
          const args = values.length ? Memory.alloc(values.length * Process.pointerSize) : ptr(0), keep = [];
          for (let i = 0; i < values.length; i++) { const value = Memory.alloc(4); value.writeS32(values[i]); keep.push(value); args.add(i * Process.pointerSize).writePointer(value); }
          const value = callRaw(entry, args);
          if (mode === 'int') return unbox(value).readS32();
          if (mode === 'string') { const utf8 = text(value); try { return utf8.readUtf8String(); } finally { free(utf8); } }
          return null;
        });
      }
    };
  }
};
