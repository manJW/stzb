'use strict';

// Detection only. No interception, method invocation, login data, or game sends.
rpc.exports = {
  inspect(expectedUsers) {
    const mono = Process.getModuleByName('libmonosgen-2.0.so');
    const bind = (name, ret, args) => new NativeFunction(mono.getExportByName(name), ret, args);
    const root = bind('mono_get_root_domain', 'pointer', []);
    const attach = bind('mono_thread_attach', 'pointer', ['pointer']);
    const detach = bind('mono_thread_detach', 'void', ['pointer']);
    const eachAssembly = bind('mono_assembly_foreach', 'void', ['pointer', 'pointer']);
    const imageOf = bind('mono_assembly_get_image', 'pointer', ['pointer']);
    const imageName = bind('mono_image_get_name', 'pointer', ['pointer']);
    const klass = bind('mono_class_from_name', 'pointer', ['pointer', 'pointer', 'pointer']);
    const fieldOf = bind('mono_class_get_field_from_name', 'pointer', ['pointer', 'pointer']);
    const methodOf = bind('mono_class_get_method_from_name', 'pointer', ['pointer', 'pointer', 'int']);
    const vtable = bind('mono_class_vtable', 'pointer', ['pointer', 'pointer']);
    const staticRead = bind('mono_field_static_get_value', 'void', ['pointer', 'pointer', 'pointer']);
    const fieldRead = bind('mono_field_get_value', 'void', ['pointer', 'pointer', 'pointer']);
    const objectClass = bind('mono_object_get_class', 'pointer', ['pointer']);
    const className = bind('mono_class_get_name', 'pointer', ['pointer']);
    const classNamespace = bind('mono_class_get_namespace', 'pointer', ['pointer']);
    const parentClass = bind('mono_class_get_parent', 'pointer', ['pointer']);
    const field = (c, name) => {
      while (!c.isNull()) { const f = fieldOf(c, Memory.allocUtf8String(name)); if (!f.isNull()) return f; c = parentClass(c); }
      return ptr(0);
    };
    const parentOf = bind('mono_class_get_parent', 'pointer', ['pointer']);
    const method = (c, name, count) => {
      while (!c.isNull()) { if (!methodOf(c, Memory.allocUtf8String(name), count).isNull()) return true; c = parentOf(c); }
      return false;
    };
    const domain = root();
    if (domain.isNull()) throw new Error('Mono domain unavailable');
    const thread = attach(domain);
    if (thread.isNull()) throw new Error('Mono inspection thread unavailable');
    try {
      const images = {};
      const pilotImages = [];
      let engineImages = 0;
      const callback = new NativeCallback(assembly => {
        const image = imageOf(assembly);
        if (image.isNull()) return;
        const name = imageName(image).readUtf8String().replace(/\.dll$/i, '');
        if (name.startsWith('STLab.')) pilotImages.push(name);
        if (name === 'Engine') engineImages++;
        if (name === 'Game.Login' || name === 'Game.Network') images[name] = image;
      }, 'void', ['pointer', 'pointer']);
      eachAssembly(callback, ptr(0));
      const result = {assemblies: Object.keys(images), ready: false, gameRequestsSent: 0, hooksInstalled: 0, pilotImages, engineImages};
      if (!images['Game.Login']) return result;
      const managerClass = klass(images['Game.Login'], Memory.allocUtf8String('Tenth.Network'), Memory.allocUtf8String('NetManager'));
      if (managerClass.isNull()) return result;
      const instanceField = field(managerClass, 'sInstance');
      const cmdField = field(managerClass, 'mCmdMgr');
      if (instanceField.isNull() || cmdField.isNull()) return result;
      const slot = Memory.alloc(Process.pointerSize);
      slot.writePointer(ptr(0));
      staticRead(vtable(domain, managerClass), instanceField, slot);
      const instance = slot.readPointer();
      if (instance.isNull()) return result;
      slot.writePointer(ptr(0));
      fieldRead(instance, cmdField, slot);
      const manager = slot.readPointer();
      if (manager.isNull()) return result;
      const managerType = objectClass(manager);
      result.managerType = classNamespace(managerType).readUtf8String() + '.' + className(managerType).readUtf8String();
      if (result.managerType !== 'Tenth.Network.DotnetCmdMgr') return result;
      const readInt = name => {
        const f = field(managerType, name);
        if (f.isNull()) return null;
        const value = Memory.alloc(Math.max(8, Process.pointerSize));
        value.writePointer(ptr(0));
        fieldRead(manager, f, value);
        const typeOf = bind('mono_field_get_type', 'pointer', ['pointer']);
        const kindOf = bind('mono_type_get_type', 'int', ['pointer']);
        const kind = kindOf(typeOf(f));
        if (kind === 14) {
          const str = value.readPointer(); if (str.isNull()) return null;
          const toUtf8 = bind('mono_string_to_utf8', 'pointer', ['pointer']);
          const free = bind('mono_free', 'void', ['pointer']);
          const text = toUtf8(str); try { const n = Number(text.readUtf8String()); return Number.isSafeInteger(n) ? n : null; } finally { free(text); }
        }
        if (kind === 8 || kind === 9 || kind === 17) return value.readS32();
        return null;
      };
      // Account binding is checked by the managed reader immediately before
      // every queued read; do not reinterpret its typed user field here.
      result.accountBinding = 'managed-component';
      result.serverId = readInt('_serverId');
      result.networkState = readInt('_networkState');
      result.sendRawObjectPresent = method(managerType, 'SendRawObject', 2);
      result.onFramePresent = method(managerType, 'OnFrame', 0);
      result.ready = result.networkState === 1 && Number.isInteger(result.serverId) && result.serverId > 0;
      return result;
    } finally {
      detach(thread);
    }
  }
};
