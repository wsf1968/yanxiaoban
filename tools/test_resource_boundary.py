from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'产品化'))
sys.path.insert(0,str(ROOT/'独立服务'))
import flight_data
import flights
import audit_clean
from core import ServiceError
from commercial_flights import FlightProvider


class ResourceBoundaryTests(unittest.TestCase):
    def test_missing_optional_table_does_not_break_disabled_provider_or_make_a_call(self):
        with patch.object(Path,'is_file',return_value=False), patch('urllib.request.urlopen',side_effect=AssertionError('No API call')):
            provider=FlightProvider()
            self.assertFalse(provider.enabled)
            with self.assertRaisesRegex(ServiceError,'资料尚未准备'):
                provider.locations.resolve('北京','city')

    def test_provider_table_cannot_reenter_source_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'独立服务/data/flight-locations.json'
            path.parent.mkdir(parents=True);path.write_text('{}')
            self.assertTrue(any(f['rule']=='provider_dataset_not_for_source_distribution' for f in audit_clean.inspect(root)['findings']))

    def test_sheet_conversion_preserves_values_and_rejects_schema_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fixture.xlsx'
            ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('xl/sharedStrings.xml',f'<sst xmlns="{ns}"><si><t>code</t></si><si><t>name</t></si><si><t>BJS</t></si><si><t> 北京 </t></si></sst>')
                z.writestr('xl/worksheets/sheet1.xml',f'<worksheet xmlns="{ns}"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row><row><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row></sheetData></worksheet>')
            self.assertEqual(flight_data.read_table(path,['code','name'],1),[['BJS',' 北京 ']])
            with self.assertRaises(ValueError):flight_data.read_table(path,['changed','name'],1)
            with self.assertRaises(ValueError):flight_data.read_table(path,['code','name'],2)

    def test_changed_reviewed_asset_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'icon.svg').write_text('changed')
            (root/'ASSET_PROVENANCE.json').write_text(json.dumps({'resources':[{'path':'icon.svg','sha256':'0'*64}]}))
            self.assertTrue(any(f['rule']=='reviewed_resource_changed' for f in audit_clean.inspect(root)['findings']))
