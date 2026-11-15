import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('desktop_builder',Path(__file__).resolve().parents[1]/'windows-install/build_desktop_installer.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class UniversalPackage(unittest.TestCase):
    def test_personal_and_example_state_cannot_enter_release(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);assets=root/'android_reader_assets';assets.mkdir()
            for name in ('identity','reader','map-selection'):
                binary=assets/(name+'.dll');binary.write_bytes(b'checked component')
                (assets/(name+'-manifest.json')).write_text(json.dumps({'file':binary.name,'sha256':builder.sha(binary)}))
            builder.verify_portable_payload(root)
            for name in ('archive-profile.json','config.json','capture-source.json','season-seed.sqlite3','data','runtime'):
                bad=root/name;bad.write_text('example account')
                with self.subTest(file=name),self.assertRaisesRegex(ValueError,'Personal/example'):builder.verify_portable_payload(root)
                bad.unlink()

    def test_reused_runtime_is_verified_and_never_copies_old_application_or_workspace(self):
        with tempfile.TemporaryDirectory()as folder:
            root=Path(folder);source=root/'old';target=root/'new';source.mkdir()
            (source/'build-manifest.json').write_text(json.dumps({'runtime_pins':builder.PINS}))
            entries=[]
            for name in ('Python/python.exe','Client/率土战场.exe','Client/resources/app/config.json','Workspace/data/private.db'):
                file=source/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(name.encode())
                entries.append(builder.sha(file)+'  '+name)
            (source/'files.sha256').write_text('\n'.join(entries),encoding='utf8')
            result=builder.reuse_runtime(source,target)
            self.assertEqual(result['verified_files'],2)
            self.assertFalse((target/'Client/resources/app').exists());self.assertFalse((target/'Workspace').exists())
            (source/'Python/python.exe').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):builder.reuse_runtime(source,root/'bad')


if __name__=='__main__':unittest.main()
