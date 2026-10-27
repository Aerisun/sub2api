import base64
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import struct
import unittest
import zlib

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from nvidia_pool.images import normalize_images,strip_png_metadata
from nvidia_pool.server import prepare_request


class ImageUploadTests(unittest.TestCase):
    def setUp(self):
        self.original=Path(__file__).with_name('fixtures').joinpath('vision-health.png').read_bytes()
        info=PngInfo();info.add_text('fixture','a'*512000)
        output=BytesIO();Image.open(BytesIO(self.original)).save(output,format='PNG',pnginfo=info)
        self.large=output.getvalue()

    def test_metadata_removal_is_pixel_identical_and_keeps_dimensions(self):
        clean=strip_png_metadata(self.large)
        self.assertLess(len(clean),len(self.large)/20)
        before,after=Image.open(BytesIO(self.large)),Image.open(BytesIO(clean))
        self.assertEqual(before.mode,after.mode);self.assertEqual(before.size,after.size)
        self.assertEqual(before.tobytes(),after.tobytes())

    def test_native_four_image_request_preserves_controls_original_and_image_detail(self):
        url='data:image/png;base64,'+base64.b64encode(self.large).decode()
        body={'model':'nvidia/nemotron-3.5-super-vl-120b-a12b','reasoning_effort':'max','messages':[{'role':'user','content':[
            {'type':'image_url','image_url':{'url':url,'detail':'high'}} for _ in range(4)]}]}
        original=deepcopy(body);out=prepare_request(body)
        self.assertEqual(body,original)
        self.assertNotIn('reasoning_effort',out)
        self.assertEqual(out['reasoning_budget'],32768)
        self.assertTrue(out['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(len(out['messages'][0]['content']),4)
        for part in out['messages'][0]['content']:
            self.assertEqual(part['image_url']['detail'],'high')
            self.assertLess(len(part['image_url']['url']),len(url)/20)

    def test_color_transparency_orientation_and_unknown_chunks_are_retained_byte_for_byte(self):
        def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        extras=b''.join(chunk(kind,b'fixture') for kind in [b'iCCP',b'gAMA',b'sRGB',b'tRNS',b'eXIf',b'vpAg'])
        # Insertion after IHDR tests exact chunk preservation, including unknown
        # future rendering metadata; it is not fed to an image decoder.
        value=self.large[:33]+extras+self.large[33:]
        clean=strip_png_metadata(value)
        self.assertIn(extras,clean)
        self.assertTrue(clean.startswith(self.original[:8]))

    def test_invalid_and_trailing_input_is_not_repaired(self):
        for value in [self.large[:-3],self.large+b'trailing',b'not PNG']:
            self.assertEqual(strip_png_metadata(value),value)

    def test_other_formats_remote_urls_and_invalid_data_are_not_reencoded(self):
        for value in ['https://example.com/image.png','data:image/jpeg;base64,'+'A'*200000,
                      'data:image/webp;base64,'+'A'*200000,'data:image/png;base64,'+'!'*200000]:
            body={'messages':[{'content':[{'type':'image_url','image_url':{'url':value}}]}]}
            self.assertIs(normalize_images(body),body)
