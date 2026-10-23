import base64
import unittest

from internal_tools.image_upload import recognition_upload
from internal_tools.test_image_tools import png
from image_hub.server import HubError


def multipart(parts):
    boundary='aerisun-test-boundary'
    body=b''
    for name,value,filename in parts:
        body+=b'--'+boundary.encode()+b'\r\nContent-Disposition: form-data; name="'+name.encode()+b'"'
        if filename:body+=b'; filename="'+filename.encode()+b'"'
        body+=b'\r\n\r\n'+value+b'\r\n'
    body+=b'--'+boundary.encode()+b'--\r\n'
    return body,'multipart/form-data; boundary='+boundary


class ImageUploadTests(unittest.TestCase):
    def test_single_file_and_utf8_prompt_are_forwarded_in_memory(self):
        raw,ct=multipart([('image',png(),'../first.png'),('prompt','只回答数字'.encode(),None),('max_output_tokens',b'512',None)])
        args=recognition_upload(raw,ct)
        self.assertEqual(args['prompt'],'只回答数字');self.assertEqual(args['max_output_tokens'],512)
        self.assertNotIn('images',args)
        self.assertEqual(base64.b64decode(args['image'].split(',',1)[1]),png())
        self.assertNotIn('filename',str(args))

    def test_rejects_bad_images_unknown_fields_and_excess_uploads(self):
        cases=[[],[('image',b'not an image','x.png')],[('image',png(),None)],
               [('image',png(),'x.png')]*2,[('image',png(),'x.png'),('model',b'secret-model',None)],
               [('image',png(),'x.png'),('prompt',b'a',None),('prompt',b'b',None)]]
        for parts in cases:
            with self.subTest(parts=len(parts)),self.assertRaises(HubError):recognition_upload(*multipart(parts))

    def test_broken_boundary_is_rejected_before_executor(self):
        with self.assertRaises(HubError):recognition_upload(b'broken','multipart/form-data; boundary=nope')

    def test_multipart_stream_is_an_explicit_boolean(self):
        for value in [b'true',b'false']:
            args=recognition_upload(*multipart([('image',png(),'x.png'),('stream',value,None)]))
            self.assertIs(args['stream'],value==b'true')
        with self.assertRaises(HubError):recognition_upload(*multipart([('image',png(),'x.png'),('stream',b'1',None)]))
