import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('portable_builder',ROOT/'windows-install/build_portable.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class PortableZipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.root=Path(self.temp.name)
        self.payload=self.root/'payload';self.payload.mkdir()
        files={'Client/率土战场.exe':b'unit-test-executable','Client/resources/app/client-config.json':b'{}',
               'Dashboard/server.py':b'pass\n','Python/python.exe':b'unit-test-python','Start-Installed.ps1':b'# launcher'}
        for kind in ('identity','reader','map-selection'):
            name=kind+'.dll';body=kind.encode()
            files['Dashboard/android_reader_assets/'+name]=body
            import hashlib
            files['Dashboard/android_reader_assets/'+kind+'-manifest.json']=json.dumps({'file':name,'sha256':hashlib.sha256(body).hexdigest()}).encode()
        for name,body in files.items():
            path=self.payload/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body)
        manifest={'version':'0.2.4','runtime_pins':builder.distribution.PINS,
                  'backend_modules':{'server.py':builder.sha(self.payload/'Dashboard/server.py')}}
        (self.payload/'build-manifest.json').write_text(json.dumps(manifest),encoding='utf8')
        self.refresh_manifest()

    def refresh_manifest(self):
        (self.payload/'files.sha256').write_text(''.join(builder.sha(p)+'  '+p.relative_to(self.payload).as_posix()+'\n'
             for p in sorted(self.payload.rglob('*'))if p.is_file()and p.name!='files.sha256'),encoding='utf8')

    def tearDown(self):self.temp.cleanup()

    def test_paths_resolve_after_moving_to_a_different_unicode_directory(self):
        output=self.root/'initial';proof=builder.prepare(self.payload,output)
        moved=self.root/'另一个位置 空格'/'便携客户端';moved.parent.mkdir();shutil.move(str(output),moved)
        app=moved/'resources/app';config=json.loads((app/'client-config.json').read_text(encoding='utf8'))
        self.assertTrue((app/config['dashboardRoot']/'server.py').resolve().is_file())
        self.assertTrue((app/config['serviceLauncher']).resolve().is_file())
        self.assertEqual((app/config['workspaceRoot']).resolve(),(moved/'Workspace').resolve())
        self.assertEqual((app/config['serviceLauncher']).resolve().parent.name,proof['program_id'])
        self.assertEqual(list((moved/'Workspace').iterdir()),[])
        self.assertFalse(proof['code_signed'])
        self.assertEqual(builder.sha(moved/'率土战场.exe'),builder.sha(self.payload/'Client/率土战场.exe'))

    def test_tampered_payload_is_rejected_before_copying(self):
        (self.payload/'Dashboard/server.py').write_text('changed')
        with self.assertRaisesRegex(ValueError,'checksum'):builder.prepare(self.payload,self.root/'bad')
        self.assertFalse((self.root/'bad').exists())

    def test_personal_config_cannot_enter_portable_distribution(self):
        (self.payload/'Dashboard/config.json').write_text('{}');self.refresh_manifest()
        with self.assertRaisesRegex(ValueError,'Personal/example'):builder.prepare(self.payload,self.root/'bad')

    def test_unverified_file_and_unsafe_paths_are_rejected(self):
        (self.payload/'surprise.exe').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError,'unverified'):builder.prepare(self.payload,self.root/'bad')
        (self.payload/'surprise.exe').unlink()
        with (self.payload/'files.sha256').open('a')as stream:stream.write('0'*64+'  ../outside.exe\n')
        with self.assertRaisesRegex(ValueError,'Unsafe'):builder.prepare(self.payload,self.root/'bad')


if __name__=='__main__':unittest.main()
