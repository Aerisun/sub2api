import copy,json,unittest
from internal_tools.vision_quality import accepts,controls
from internal_tools.vision_monitor import health_result

class VisionQualityTests(unittest.TestCase):
 def setUp(self):
  self.good={'rows':[['SSH 隧道','56.7','134.2','99.95%','主用'],['HTTPS 备用','318.6','746.9','98.70%','备用'],['Embedding','857','1324','99.10%','远端 CPU'],['Rerank','536','980','99.30%','远端 CPU']], 'bar_colors':['蓝色','红色','绿色','橙色'],'unit':'ms','gpu':None,'cost':None}
 def test_correct_extraction_passes_even_with_numeric_cells_or_markdown(self):
  self.assertTrue(accepts(json.dumps(self.good)))
  self.good['rows'][0][1]=56.7
  self.assertTrue(accepts('```json\n'+json.dumps(self.good)+'\n```'))
 def test_wrong_row_correspondence_decimal_percent_or_missing_detail_fails(self):
  for edit in [lambda x:x['rows'][0].__setitem__(1,'567'),lambda x:x['rows'][0].__setitem__(2,'746.9'),lambda x:x['rows'][1].__setitem__(3,'98.70'),lambda x:x['rows'].pop(),lambda x:x['bar_colors'].__setitem__(2,'红色'),lambda x:x.__setitem__('gpu','H100'),lambda x:x.pop('cost')]:
   bad=copy.deepcopy(self.good);edit(bad);self.assertFalse(accepts(json.dumps(bad)))
 def test_plain_description_is_not_quality_recovery_proof(self):
  self.assertFalse(accepts('图片有一个表格，SSH 很快。'))
  self.assertFalse(accepts('{}'))
 def test_correct_complete_slow_answer_is_available_with_explicit_latency_miss(self):
  self.assertEqual(health_result(json.dumps(self.good),[8100],True),(True,'verified_slow'))
  self.assertEqual(health_result(json.dumps(self.good),[2100],True),(True,'verified'))
  self.assertFalse(health_result(json.dumps(self.good),[2100],False)[0])
  self.assertFalse(health_result('{}',[2100],True)[0])
 def test_verification_budget_is_bounded_and_does_not_consume_tiny_answers(self):
  self.assertEqual(controls('nvidia/nemotron-3.5-super-vl-120b-a12b',3072),{'reasoning_effort':'minimal','reasoning_budget':256})
  for model,limit in [('nvidia/nemotron-3.5-super-vl-120b-a12b',16),('nvidia/nemotron-3-nano-omni-30b-a3b-reasoning',3072)]:
   self.assertEqual(controls(model,limit),{'reasoning_effort':'off'})
