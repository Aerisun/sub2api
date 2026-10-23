"""Known-answer Chinese table check for vision recovery; no user data."""
from decimal import Decimal, InvalidOperation
import json

QUALITY_PROMPT=(
    '只输出 JSON 对象，不要 Markdown。rows 按表格原顺序列出四行，每行是'
    '[链路名,P50,P95,成功率,状态]，均使用字符串，百分数保留百分号。'
    'bar_colors 是柱状图从上到下的四种颜色，使用中文颜色名称，不要 HEX 或 RGB 代码；unit 是延迟单位，'
    'gpu 和 cost 是图中提供的 GPU 型号和费用，未提供时使用 null。'
    '不要包含表头行。'
)
EXPECTED=[('ssh','56.7','134.2','99.95','主用'),
          ('https','318.6','746.9','98.70','备用'),
          ('embedding','857','1324','99.10','远端cpu'),
          ('rerank','536','980','99.30','远端cpu')]

def controls(model,limit):
    # A bounded verification pass fixes contradictory counts without an open
    # reasoning budget. Very short explicit answers keep their token allowance.
    if model=='nvidia/nemotron-3.5-super-vl-120b-a12b' and limit>=512:
        return {'reasoning_effort':'minimal','reasoning_budget':256}
    return {'reasoning_effort':'off'}

def accepts(text):
    if not isinstance(text,str):return False
    try:
        text=text.strip()
        if text.startswith('```'):
            text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
        value=json.loads(text);rows=value['rows']
        if not isinstance(rows,list) or len(rows)!=4:return False
        for row,(name,p50,p95,success,status) in zip(rows,EXPECTED):
            if not isinstance(row,list) or len(row)!=5:return False
            if name not in str(row[0]).lower():return False
            if Decimal(str(row[1]))!=Decimal(p50) or Decimal(str(row[2]))!=Decimal(p95):return False
            percentage=str(row[3]).strip()
            if not percentage.endswith('%') or Decimal(percentage[:-1])!=Decimal(success):return False
            if status not in ''.join(str(row[4]).lower().split()):return False
        colors=value['bar_colors']
        alternatives=[('蓝','blue'),('红','red'),('绿','green'),('橙','黄','orange','amber','yellow')]
        return (isinstance(colors,list) and len(colors)==4 and
            all(any(word in str(color).lower() for word in allowed) for color,allowed in zip(colors,alternatives)) and
            str(value['unit']).lower()=='ms' and 'gpu' in value and value['gpu'] is None and
            'cost' in value and value['cost'] is None)
    except (ValueError,TypeError,KeyError,InvalidOperation):return False
