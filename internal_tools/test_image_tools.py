import base64,json,unittest,zlib,struct
from unittest.mock import patch
from image_hub.server import HubError,NATIVE_MODEL_CHOICES
from internal_tools import registry
from internal_tools.image_tools import ImageGenerationExecutor,ImageRecognitionExecutor,GENERATION_DEFINITION,RECOGNITION_DEFINITION

def png():
 def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
 raw=b'\x00'+b'\xff\x00\x00'*2
 return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')
IMAGE='data:image/png;base64,'+base64.b64encode(png()).decode()
TASK='imgtask_'+'1'*32

class ImageToolTests(unittest.TestCase):
 def test_catalogue_is_key_scoped_and_has_no_image_provider_choices(self):
  with patch('internal_tools.registry.authorized_models',return_value={'image_generation','image_recognition','web_search'}):
   defs=registry.catalogue('sk-user','http://gateway')
  self.assertEqual({item['name'] for item in defs},{'image_generation','image_recognition','web_search'})
  self.assertEqual(GENERATION_DEFINITION['inputSchema']['properties']['model']['enum'],list(NATIVE_MODEL_CHOICES))
  with patch('internal_tools.registry.authorized_models',return_value={'image_generation'}):
   self.assertEqual([item['name'] for item in registry.catalogue('sk-user','http://gateway')],['image_generation'])

 def test_image_submission_and_poll_are_same_owned_task_without_resubmission(self):
  captured=[]
  def request(url,key,body=None,**kwargs):
   captured.append((url,key,body,kwargs))
   if body is not None:return 202,{'task_id':TASK,'status':'processing','model':NATIVE_MODEL_CHOICES[0],'poll_url':'/v1/images/tasks/'+TASK}
   return 200,{'task_id':TASK,'status':'completed','model':NATIVE_MODEL_CHOICES[0],'result':{'model':NATIVE_MODEL_CHOICES[0],'data':[{'url':'https://example.com/output.png'}]}}
  with patch('internal_tools.image_tools.authorized_models',return_value={'image_generation'}),patch('internal_tools.image_tools.request_json',side_effect=request):
   run=ImageGenerationExecutor('sk-customer',headers={'X-Real-IP':'203.0.113.1'})
   submitted=run({'prompt':'a circle','idempotency_key':'stable-task'},'one')
   completed=run({'task_id':TASK},'two')
  self.assertEqual(submitted['status'],'processing');self.assertEqual(completed['status'],'completed')
  self.assertEqual(captured[0][3]['headers']['Idempotency-Key'],'stable-task')
  self.assertEqual(captured[0][2]['imageSize'],'1K')
  self.assertEqual(sum(body is not None for _,_,body,_ in captured),1)
  self.assertTrue(all(key=='sk-customer' for _,key,_,_ in captured))
  self.assertEqual(submitted['output']['model'],NATIVE_MODEL_CHOICES[0])
  self.assertEqual(completed['output']['model'],NATIVE_MODEL_CHOICES[0])
  self.assertNotIn('model',completed['output']['result'])
  self.assertEqual(completed['usage']['submissions'],0)
  self.assertEqual(submitted['next_call']['arguments']['task_id'],TASK)

 def test_explicit_choice_is_forwarded_and_cannot_broaden_a_legacy_key(self):
  for choice in NATIVE_MODEL_CHOICES:
   with self.subTest(choice=choice),patch('internal_tools.image_tools.authorized_models',return_value={choice}),patch('internal_tools.image_tools.request_json',return_value=(202,{'task_id':TASK,'status':'processing','model':choice})) as request:
    result=ImageGenerationExecutor('sk-user')({'model':choice,'prompt':'draw'},'one')
    self.assertEqual(request.call_args.args[2]['model'],choice)
    self.assertEqual(result['output']['model'],choice)
  with patch('internal_tools.image_tools.authorized_models',return_value={NATIVE_MODEL_CHOICES[2]}),patch('internal_tools.image_tools.request_json') as request,self.assertRaises(HubError) as err:
   ImageGenerationExecutor('sk-user')({'model':NATIVE_MODEL_CHOICES[0],'prompt':'draw'},'one')
  self.assertEqual(err.exception.status,403);request.assert_not_called()

 def test_bad_or_forbidden_generation_never_submits(self):
  for args in ({'prompt':'draw','model':'unsupported-image-model'},{'prompt':''},{'prompt':'draw','image':['not-an-image']},{'task_id':TASK,'prompt':'repeat'},{'prompt':'draw','n':5}):
   with self.subTest(args=args),patch('internal_tools.image_tools.request_json') as request,self.assertRaises(HubError):ImageGenerationExecutor('sk-user')(args,'one')
   request.assert_not_called()
  with patch('internal_tools.image_tools.authorized_models',return_value={'qwen3.8-flash'}),patch('internal_tools.image_tools.request_json') as request,self.assertRaises(HubError) as error:
   ImageGenerationExecutor('sk-user')({'prompt':'draw'},'one')
  self.assertEqual(error.exception.status,403);request.assert_not_called()

 def test_failed_submission_is_never_automatically_retried(self):
  with patch('internal_tools.image_tools.authorized_models',return_value={'image_generation'}),patch('internal_tools.image_tools.request_json',side_effect=HubError(504,'uncertain')) as request,self.assertRaises(HubError):
   ImageGenerationExecutor('sk-user')({'prompt':'draw','idempotency_key':'stable'},'one')
  self.assertEqual(request.call_count,1)

 def test_recognition_preserves_real_usage_and_uses_narrow_gateway_route(self):
  raw={'choices':[{'message':{'content':'红色'}}],'usage':{'prompt_tokens':20,'completion_tokens':2,'total_tokens':22,'prompt_tokens_details':{'cached_tokens':3}}}
  with patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}),patch('internal_tools.image_tools.recognition_request',return_value=(200,raw)) as request:
   result=ImageRecognitionExecutor('sk-user')({'image':IMAGE,'prompt':'颜色？'},'one')
  self.assertTrue(request.call_args.args[0].endswith('/v1/images/recognitions'))
  self.assertEqual(request.call_args.args[2]['model'],'nvidia/nemotron-3.5-super-vl-120b-a12b')
  self.assertEqual(request.call_args.args[2]['reasoning_effort'],'minimal')
  self.assertEqual(request.call_args.args[2]['reasoning_budget'],256)
  self.assertEqual(request.call_args.args[2]['messages'][-1]['content'][0]['text'],'颜色？')
  self.assertIn('OCR 保留原文和结构',request.call_args.args[2]['messages'][0]['content'])
  self.assertEqual(request.call_args.args[1],'sk-user');self.assertEqual(result['usage'],raw['usage'])
  self.assertEqual(result['output']['text'],'红色');self.assertNotIn(IMAGE,json.dumps(result))
  self.assertNotIn('model',RECOGNITION_DEFINITION['inputSchema']['properties'])

 def test_legacy_vision_only_key_keeps_its_existing_model_scope(self):
  raw={'choices':[{'message':{'content':'红色'}}],'usage':{'prompt_tokens':20,'completion_tokens':2,'total_tokens':22}}
  with patch('internal_tools.image_tools.authorized_models',return_value={'glm-5.3-flash'}),patch('internal_tools.image_tools.recognition_request',return_value=(200,raw)) as request:
   ImageRecognitionExecutor('sk-legacy')({'image':IMAGE},'one')
  self.assertEqual(request.call_args.args[2]['model'],'glm-5.3-flash')
  self.assertEqual(request.call_args.args[1],'sk-legacy')

 def test_recognition_validation_blocks_private_urls_and_malformed_images(self):
  for image in ('http://127.0.0.1/x','file:///etc/passwd','data:image/png;base64,ZmFrZQ==','https://u:p@example.com/x'):
   with self.subTest(image=image),patch('internal_tools.image_tools.recognition_request') as request,self.assertRaises(HubError):
    ImageRecognitionExecutor('sk-user')({'image':image},'one')
   request.assert_not_called()

 def test_recognition_rejects_multiple_images_before_upstream_or_billing(self):
  for args in ({'images':[]},{'images':[IMAGE,IMAGE]},{'image':IMAGE,'images':[IMAGE]}):
   with self.subTest(args=list(args)),patch('internal_tools.image_tools.recognition_request') as request,patch('internal_tools.image_tools.authorized_models') as auth,self.assertRaises(HubError) as error:
    ImageRecognitionExecutor('sk-user')(args,'single')
   self.assertEqual(error.exception.status,400);request.assert_not_called();auth.assert_not_called()
  self.assertEqual(RECOGNITION_DEFINITION['inputSchema']['properties']['images']['maxItems'],1)

 def test_single_image_compatibility_alias_keeps_one_image_and_one_charge(self):
  raw={'choices':[{'message':{'content':'红色'}}],'usage':{'prompt_tokens':20,'completion_tokens':2,'total_tokens':22}}
  with patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}),patch('internal_tools.image_tools.recognition_request',return_value=(200,raw)) as request:
   result=ImageRecognitionExecutor('sk-user')({'images':[IMAGE]},'one')
  self.assertEqual(sum(p['type']=='image_url' for p in request.call_args.args[2]['messages'][-1]['content']),1)
  self.assertEqual(result['output']['image_count'],1);self.assertEqual(result['billing']['images'],1)

 def test_recognition_does_not_fabricate_missing_usage_or_retry(self):
  with patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}),patch('internal_tools.image_tools.recognition_request',return_value=(200,{'choices':[{'message':{'content':'red'}}]})) as request,self.assertRaises(HubError):
   ImageRecognitionExecutor('sk-user')({'image':IMAGE},'one')
  self.assertEqual(request.call_count,1)

if __name__=='__main__':unittest.main()

class NvidiaVisionFailoverTests(unittest.TestCase):
 def test_confirmed_unbilled_reasoning_failure_recovers_without_repeating_visible_output(self):
  error=HubError(502,'provider failed');error.semantic_started=True;error.retryable_unbilled=True
  value={'choices':[{'message':{'content':'red'}}],'usage':{'total_tokens':11}}
  with patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}),patch('internal_tools.image_tools.recognition_request',side_effect=[error,(200,value)]) as request:
   result=ImageRecognitionExecutor('sk-customer')({'image':IMAGE},'one')
  self.assertEqual(request.call_count,2);self.assertEqual(result['billing']['images'],1)

 def test_pre_output_busy_retries_same_quality_model_with_same_customer_auth(self):
  error=HubError(503,'busy');error.semantic_started=False
  value={'choices':[{'message':{'content':'red'}}],'usage':{'prompt_tokens':10,'completion_tokens':1,'total_tokens':11}}
  with patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}),patch('internal_tools.image_tools.recognition_request',side_effect=[error,(200,value)]) as request:
   result=ImageRecognitionExecutor('sk-customer')({'image':IMAGE},'one')
  self.assertEqual(request.call_count,2)
  self.assertEqual([c.args[2]['model'] for c in request.call_args_list],['nvidia/nemotron-3.5-super-vl-120b-a12b','nvidia/nemotron-3.5-super-vl-120b-a12b'])
  self.assertTrue(all(c.args[1]=='sk-customer' for c in request.call_args_list));self.assertEqual(result['billing']['images'],1)
 def test_partial_or_uncertain_response_is_not_replayed(self):
  for started in [True,None]:
   error=HubError(502,'incomplete')
   if started is not None:error.semantic_started=started
   with patch('internal_tools.image_tools.authorized_models',return_value={'image_recognition'}),patch('internal_tools.image_tools.recognition_request',side_effect=error) as request,self.assertRaises(HubError):
    ImageRecognitionExecutor('sk-customer')({'image':IMAGE},'one')
   self.assertEqual(request.call_count,1)

# Routing tests use request-local temporary databases; never touch production health.
def isolated_router(self):
 import tempfile
 from pathlib import Path
 from internal_tools.vision_routing import VisionRouter
 from image_hub.concurrency import FileSlotPool
 directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
 mock=patch('internal_tools.image_tools.ROUTER',VisionRouter(Path(directory.name)/'routing.db'));mock.start();self.addCleanup(mock.stop)
 for name,limit,folder in [('RECOGNITION_SLOTS',64,'accepted'),('RECOGNITION_UPSTREAM_SLOTS',16,'upstream')]:
  mock=patch('internal_tools.image_tools.'+name,FileSlotPool(limit,Path(directory.name)/folder));mock.start();self.addCleanup(mock.stop)
ImageToolTests.setUp=isolated_router
NvidiaVisionFailoverTests.setUp=isolated_router
